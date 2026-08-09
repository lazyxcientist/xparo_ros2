"""Covers remote_ops.py's transport-agnostic exec/file-transfer/teleop
handlers -- ported from tethered_module's jetson_tcp_node_d_m.py. These are
exercised directly here (not through Engine/a transport) since the whole
point of the port is that they don't know or care which transport is
driving them; test_engine.py covers the dispatch wiring on top of this.
"""
import base64
import os
import time

import pytest

from xparo import remote_ops


# ------------------------------------------------------------------
# RUN_COMMAND
# ------------------------------------------------------------------
def test_clamp_command_timeout_defaults_and_bounds():
    assert remote_ops.clamp_command_timeout(None) == remote_ops.DEFAULT_COMMAND_TIMEOUT_SEC
    assert remote_ops.clamp_command_timeout("not-a-number") == remote_ops.DEFAULT_COMMAND_TIMEOUT_SEC
    assert remote_ops.clamp_command_timeout(0) == 1.0  # floored
    assert remote_ops.clamp_command_timeout(99999) == remote_ops.MAX_COMMAND_TIMEOUT_SEC
    assert remote_ops.clamp_command_timeout(15) == 15.0


def test_handle_run_command_success():
    responses = []
    remote_ops.handle_run_command("echo hello", "req-1", 5.0, responses.append)

    assert len(responses) == 1
    result = responses[0]["COMMAND_RESULT"]
    assert result["request_id"] == "req-1"
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert "hello" in result["output"]


def test_handle_run_command_nonzero_exit_is_not_success():
    responses = []
    remote_ops.handle_run_command("exit 7", "req-2", 5.0, responses.append)
    result = responses[0]["COMMAND_RESULT"]
    assert result["exit_code"] == 7
    assert result["success"] is False


def test_handle_run_command_timeout():
    responses = []
    remote_ops.handle_run_command("sleep 5", "req-3", 0.2, responses.append)
    result = responses[0]["COMMAND_RESULT"]
    assert result["timed_out"] is True
    assert result["success"] is False
    assert "timeout" in result["output"]


def test_handle_run_command_truncates_to_tail():
    cmd = "python3 -c \"[print(i) for i in range(200)]\""
    responses = []
    remote_ops.handle_run_command(cmd, "req-4", 10.0, responses.append)
    result = responses[0]["COMMAND_RESULT"]
    lines = result["output"].splitlines()
    assert result["truncated"] is True
    assert len(lines) == remote_ops.RUN_COMMAND_MAX_LINES
    # Tail, not head -- the most recent lines survive.
    assert lines[-1] == "199"


# ------------------------------------------------------------------
# Teleop
# ------------------------------------------------------------------
def test_handle_teleop_pads_short_payload():
    joy_calls = []
    responses = []
    remote_ops.handle_teleop([1.0], [], lambda a, b: joy_calls.append((a, b)), responses.append)

    axes, buttons = joy_calls[0]
    assert len(axes) == remote_ops.MIN_JOY_AXES
    assert len(buttons) == remote_ops.MIN_JOY_BUTTONS
    assert axes[0] == 1.0
    assert responses[0] == {"TELEOP_ACK": {"success": True}}


def test_handle_teleop_does_not_truncate_longer_payload():
    joy_calls = []
    remote_ops.handle_teleop(
        [1.0, 2.0, 3.0, 4.0, 5.0], [1, 0, 1, 1], lambda a, b: joy_calls.append((a, b)), lambda r: None,
    )
    axes, buttons = joy_calls[0]
    assert axes == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert buttons == [1, 0, 1, 1]


def test_handle_teleop_coerces_types():
    joy_calls = []
    remote_ops.handle_teleop(["0.5", 1], [True, 0, "1"], lambda a, b: joy_calls.append((a, b)), lambda r: None)
    axes, buttons = joy_calls[0]
    assert axes[0] == 0.5 and isinstance(axes[0], float)
    assert buttons[0] == 1 and isinstance(buttons[0], int)


# ------------------------------------------------------------------
# File listing / deletion
# ------------------------------------------------------------------
def test_list_files_reports_tree(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    sub = tmp_path / "bags"
    sub.mkdir()
    (sub / "run1.mcap").write_bytes(b"1234")

    tree = remote_ops.list_files(str(tmp_path))
    names = {e["name"]: e for e in tree}
    assert names["a.txt"]["type"] == "file"
    assert names["a.txt"]["size"] == 2
    assert names["bags"]["type"] == "folder"
    assert names["bags"]["children"][0]["path"] == "bags/run1.mcap"


def test_handle_list_files_sends_file_list(tmp_path):
    responses = []
    remote_ops.handle_list_files(str(tmp_path), responses.append)
    assert responses == [{"FILE_LIST": {"tree": []}}]


def test_handle_delete_file_success(tmp_path):
    target = tmp_path / "doomed.txt"
    target.write_text("bye")
    responses = []
    remote_ops.handle_delete_file(str(tmp_path), "doomed.txt", responses.append)
    assert responses[0] == {"DELETE_ACK": {"success": True, "path": "doomed.txt"}}
    assert not target.exists()


def test_handle_delete_file_path_traversal_denied(tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret")
    try:
        responses = []
        remote_ops.handle_delete_file(str(tmp_path), "../outside_secret.txt", responses.append)
        assert responses[0]["DELETE_ACK"]["success"] is False
        assert "traversal" in responses[0]["DELETE_ACK"]["message"].lower()
        assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_handle_delete_file_not_found(tmp_path):
    responses = []
    remote_ops.handle_delete_file(str(tmp_path), "nope.txt", responses.append)
    assert responses[0]["DELETE_ACK"]["success"] is False
    assert "not found" in responses[0]["DELETE_ACK"]["message"].lower()


def test_handle_delete_file_refuses_directories(tmp_path):
    (tmp_path / "a_dir").mkdir()
    responses = []
    remote_ops.handle_delete_file(str(tmp_path), "a_dir", responses.append)
    assert responses[0]["DELETE_ACK"]["success"] is False
    assert (tmp_path / "a_dir").exists()


# ------------------------------------------------------------------
# File transfer session (base64-in-JSON adaptation of FileTransferHandler)
# ------------------------------------------------------------------
def test_upload_round_trip(tmp_path):
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []

    session.handle_file_req({"filename": "incoming.bin", "direction": "upload", "size": 11}, responses.append)
    assert responses[-1] == {"FILE_REQ": {"filename": "incoming.bin", "direction": "upload", "ready": True}}

    session.handle_file_chunk({"data": base64.b64encode(b"hello ").decode()})
    session.handle_file_chunk({"data": base64.b64encode(b"world").decode()})
    session.handle_file_complete(responses.append)

    assert responses[-1] == {"FILE_COMPLETE": {"status": "ok", "expected": 11, "received": 11}}
    assert (tmp_path / "incoming.bin").read_bytes() == b"hello world"


def test_upload_filename_is_basenamed(tmp_path):
    """filename in the request is untrusted wire input -- Path(...).name
    strips any directory components, same guard as _delete_jetson_file."""
    session = remote_ops.FileTransferSession(str(tmp_path))
    session.handle_file_req({"filename": "../../etc/evil.bin", "direction": "upload", "size": 1}, lambda r: None)
    session.handle_file_chunk({"data": base64.b64encode(b"x").decode()})
    session.handle_file_complete(lambda r: None)
    assert (tmp_path / "evil.bin").exists()
    assert not (tmp_path.parent.parent / "etc" / "evil.bin").exists()


def test_download_round_trip(tmp_path):
    (tmp_path / "existing.bin").write_bytes(b"a" * 200000)  # > FILE_CHUNK_SIZE, forces multiple chunks
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "existing.bin", "direction": "download"}, responses.append)

    req_msgs = [r["FILE_REQ"] for r in responses if "FILE_REQ" in r]
    chunk_msgs = [r["FILE_CHUNK"] for r in responses if "FILE_CHUNK" in r]
    complete_msgs = [r["FILE_COMPLETE"] for r in responses if "FILE_COMPLETE" in r]

    assert req_msgs[0]["size"] == 200000
    assert len(chunk_msgs) > 1  # confirms it actually chunked, not one giant blob
    reassembled = b"".join(base64.b64decode(c["data"]) for c in chunk_msgs)
    assert reassembled == b"a" * 200000
    assert complete_msgs[0]["expected"] == 200000


def test_download_missing_file_sends_error(tmp_path):
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "nope.bin", "direction": "download"}, responses.append)
    assert "error" in responses[0]


def test_download_path_traversal_denied(tmp_path):
    outside = tmp_path.parent / "topsecret.bin"
    outside.write_bytes(b"nope")
    try:
        session = remote_ops.FileTransferSession(str(tmp_path))
        responses = []
        session.handle_file_req({"filename": "../topsecret.bin", "direction": "download"}, responses.append)
        assert "error" in responses[0]
        assert "traversal" in responses[0]["error"]["message"].lower()
    finally:
        outside.unlink(missing_ok=True)


def test_chunk_without_a_pending_upload_is_a_safe_noop(tmp_path):
    session = remote_ops.FileTransferSession(str(tmp_path))
    # No handle_file_req called first -- must not raise.
    session.handle_file_chunk({"data": base64.b64encode(b"stray").decode()})
    session.handle_file_complete(lambda r: None)


def test_only_one_upload_tracked_at_a_time(tmp_path):
    """Matches the original's documented single-transfer behavior -- a
    second FILE_REQ(upload) simply replaces the tracked upload state."""
    session = remote_ops.FileTransferSession(str(tmp_path))
    session.handle_file_req({"filename": "first.bin", "direction": "upload", "size": 1}, lambda r: None)
    session.handle_file_req({"filename": "second.bin", "direction": "upload", "size": 1}, lambda r: None)
    session.handle_file_chunk({"data": base64.b64encode(b"x").decode()})
    session.handle_file_complete(lambda r: None)

    assert (tmp_path / "second.bin").read_bytes() == b"x"
    assert not (tmp_path / "first.bin").exists() or (tmp_path / "first.bin").stat().st_size == 0
