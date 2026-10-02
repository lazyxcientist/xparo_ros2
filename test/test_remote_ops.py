"""Covers remote_ops.py's transport-agnostic exec/file-transfer/teleop
handlers -- ported from tethered_module's jetson_tcp_node_d_m.py. These are
exercised directly here (not through Engine/a transport) since the whole
point of the port is that they don't know or care which transport is
driving them; test_engine.py covers the dispatch wiring on top of this.
"""
import base64
import math
import os
import subprocess
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

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
        [1.0, -1.0, 0.5, -0.5, 0.25], [1, 0, 1, 1], lambda a, b: joy_calls.append((a, b)), lambda r: None,
    )
    axes, buttons = joy_calls[0]
    assert axes == [1.0, -1.0, 0.5, -0.5, 0.25]
    assert buttons == [1, 0, 1, 1]


def test_handle_teleop_coerces_types():
    joy_calls = []
    remote_ops.handle_teleop(["0.5", 1], [True, 0, "1"], lambda a, b: joy_calls.append((a, b)), lambda r: None)
    axes, buttons = joy_calls[0]
    assert axes[0] == 0.5 and isinstance(axes[0], float)
    assert buttons[0] == 1 and isinstance(buttons[0], int)


# 2026-09-29 stress test finding F13 (MEDIUM): confirmed live -- a TELEOP
# payload of {"axes": [Infinity, -Infinity, NaN, 99999999.9],
# "buttons": [1,0,1]} was published straight to the real Joy topic with
# zero validation. Downstream fin/thruster control expects a normalized
# [-1.0, 1.0] axis; a NaN or huge value reaching an actuator is a hardware
# safety issue.
def test_handle_teleop_clamps_out_of_range_axes():
    joy_calls = []
    remote_ops.handle_teleop(
        [99999999.9, -99999999.9, 1.5, -1.5], [], lambda a, b: joy_calls.append((a, b)), lambda r: None,
    )
    axes, _ = joy_calls[0]
    assert axes == [1.0, -1.0, 1.0, -1.0]


def test_handle_teleop_neutralizes_non_finite_axes():
    joy_calls = []
    remote_ops.handle_teleop(
        [float("inf"), float("-inf"), float("nan"), 0.3],
        [1, 0, 1], lambda a, b: joy_calls.append((a, b)), lambda r: None,
    )
    axes, _ = joy_calls[0]
    assert axes == [0.0, 0.0, 0.0, 0.3]


def test_handle_teleop_reproduces_the_exact_confirmed_exploit_payload():
    """The exact payload confirmed live to reach the real Joy topic
    unvalidated: {"axes": [Infinity, -Infinity, NaN, 99999999.9],
    "buttons": [1,0,1]}."""
    joy_calls = []
    responses = []
    remote_ops.handle_teleop(
        [float("inf"), float("-inf"), float("nan"), 99999999.9],
        [1, 0, 1], lambda a, b: joy_calls.append((a, b)), responses.append,
    )
    axes, buttons = joy_calls[0]
    assert all(math.isfinite(a) for a in axes)
    assert all(-1.0 <= a <= 1.0 for a in axes)
    assert axes == [0.0, 0.0, 0.0, 1.0]
    assert buttons == [1, 0, 1]
    assert responses[0] == {"TELEOP_ACK": {"success": True}}  # still acked, not dropped


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
    # base_dir lets the file browser show exactly where on the robot's
    # filesystem these files live (e.g. to cd there over a Terminal
    # session) -- always the resolved absolute path, regardless of
    # whether tmp_path itself was passed in as one.
    assert responses == [{"FILE_LIST": {"tree": [], "base_dir": str(Path(tmp_path).resolve())}}]


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


def test_handle_delete_file_denies_sibling_directory_prefix_match(tmp_path):
    """A bare startswith(base) guard (the pattern this was ported from)
    would wrongly accept a sibling directory whose name happens to share
    base's string as a prefix, e.g. base=".../data" matching
    ".../data_evil/x" -- confirms the os.sep-boundary check actually closes
    that, not just the plain "../" case the other traversal test covers.
    """
    base = tmp_path / "data"
    base.mkdir()
    sibling = tmp_path / "data_evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret")
    try:
        responses = []
        remote_ops.handle_delete_file(str(base), "../data_evil/secret.txt", responses.append)
        assert responses[0]["DELETE_ACK"]["success"] is False
        assert "traversal" in responses[0]["DELETE_ACK"]["message"].lower()
        assert (sibling / "secret.txt").exists()
    finally:
        (sibling / "secret.txt").unlink(missing_ok=True)


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


# 2026-09-28 stress test finding F7 (HIGH): confirmed live -- a FILE_REQ
# declaring size=10 bytes, followed by 5 MiB of FILE_CHUNK data, was
# written to disk in full with no limit at all, and FILE_COMPLETE reported
# {"status": "ok", "expected": 10, "received": 5242880} instead of
# rejecting the mismatch.
def test_sending_far_more_than_the_declared_size_is_aborted_not_written_in_full(tmp_path):
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "small_claim.bin", "direction": "upload", "size": 10}, responses.append)

    oversized_chunk = b"x" * (1024 * 1024)  # 1 MiB, way past the declared 10 bytes
    session.handle_file_chunk({"data": base64.b64encode(oversized_chunk).decode()}, responses.append)

    assert "error" in responses[-1]
    assert "limit" in responses[-1]["error"]["message"]
    # The partial/oversized write was discarded, not left on disk looking
    # like a normal file.
    assert not (tmp_path / "small_claim.bin").exists()
    # The session is clean -- a FILE_COMPLETE after an abort is a no-op,
    # not a crash or a stale "ok".
    session.handle_file_complete(responses.append)
    assert responses[-1]["error"]["message"].startswith("upload exceeded")  # unchanged since the abort


def test_a_declared_size_over_the_hard_cap_is_refused_before_any_file_is_created(tmp_path):
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req(
        {"filename": "huge.bin", "direction": "upload", "size": remote_ops.MAX_UPLOAD_SIZE_BYTES + 1},
        responses.append,
    )
    assert "error" in responses[-1]
    assert "too large" in responses[-1]["error"]["message"]
    assert not (tmp_path / "huge.bin").exists()
    assert session._current_upload is None  # never even started


def test_uploading_more_than_the_hard_cap_is_aborted_even_with_no_declared_size(tmp_path):
    """The declared `size` was confirmed live to be pure decoration -- this
    covers the OTHER half: a sender that declares nothing (or 0) is still
    bounded by the hard cap, not unlimited."""
    session = remote_ops.FileTransferSession(str(tmp_path))
    session.handle_file_req({"filename": "undeclared.bin", "direction": "upload"}, lambda r: None)

    chunk = b"y" * (1024 * 1024)
    sent_total = 0
    aborted_at = None
    responses = []
    # A real attacker would just keep sending; stop as soon as it aborts
    # rather than actually writing 200+ MiB in a unit test.
    while sent_total < remote_ops.MAX_UPLOAD_SIZE_BYTES + (2 * 1024 * 1024):
        session.handle_file_chunk({"data": base64.b64encode(chunk).decode()}, responses.append)
        sent_total += len(chunk)
        if responses and "error" in responses[-1]:
            aborted_at = sent_total
            break
    assert aborted_at is not None, "never aborted -- the hard cap did nothing"
    assert not (tmp_path / "undeclared.bin").exists()


def test_a_size_mismatch_on_complete_is_reported_as_an_error_and_the_file_is_discarded(tmp_path):
    """Even when the total stays under every cap, FILE_COMPLETE must not
    silently say "ok" for a transfer that doesn't match what it promised
    -- a truncated or short transfer is exactly as much a lie as an
    oversized one."""
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "short.bin", "direction": "upload", "size": 100}, responses.append)
    session.handle_file_chunk({"data": base64.b64encode(b"only ten!!").decode()}, responses.append)  # 10, not 100
    session.handle_file_complete(responses.append)

    result = responses[-1]["FILE_COMPLETE"]
    assert result["status"] == "error"
    assert result["expected"] == 100
    assert result["received"] == 10
    assert not (tmp_path / "short.bin").exists()


def test_a_matching_transfer_still_reports_ok_exactly_as_before(tmp_path):
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "exact.bin", "direction": "upload", "size": 5}, responses.append)
    session.handle_file_chunk({"data": base64.b64encode(b"exact").decode()}, responses.append)
    session.handle_file_complete(responses.append)

    assert responses[-1] == {"FILE_COMPLETE": {"status": "ok", "expected": 5, "received": 5}}
    assert (tmp_path / "exact.bin").read_bytes() == b"exact"


def test_no_declared_size_at_all_is_not_treated_as_a_mismatch(tmp_path):
    """expected_size == 0 means "nothing to check against" (not every real
    caller sets it) -- must not be misread as "received 0 bytes was
    expected" and falsely flagged as a mismatch."""
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "no_size_given.bin", "direction": "upload"}, responses.append)
    session.handle_file_chunk({"data": base64.b64encode(b"some data").decode()}, responses.append)
    session.handle_file_complete(responses.append)

    assert responses[-1]["FILE_COMPLETE"]["status"] == "ok"
    assert (tmp_path / "no_size_given.bin").exists()


def test_malformed_chunk_data_aborts_and_reports_an_error_instead_of_silently_vanishing(tmp_path):
    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "bad_b64.bin", "direction": "upload", "size": 5}, responses.append)
    session.handle_file_chunk({"data": "not valid base64!!!"}, responses.append)

    assert "error" in responses[-1]
    assert session._current_upload is None
    assert not (tmp_path / "bad_b64.bin").exists()


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


def test_download_denies_sibling_directory_prefix_match(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    sibling = tmp_path / "data_evil"
    sibling.mkdir()
    (sibling / "secret.bin").write_bytes(b"nope")
    try:
        session = remote_ops.FileTransferSession(str(base))
        responses = []
        session.handle_file_req({"filename": "../data_evil/secret.bin", "direction": "download"}, responses.append)
        assert "error" in responses[0]
        assert "traversal" in responses[0]["error"]["message"].lower()
    finally:
        (sibling / "secret.bin").unlink(missing_ok=True)


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


# ------------------------------------------------------------------
# Folder download -- zipped server-side, then streamed through the exact
# same chunked FILE_REQ/FILE_CHUNK/FILE_COMPLETE sequence as a single file.
# ------------------------------------------------------------------
def test_downloading_a_folder_zips_it_and_streams_the_zip(tmp_path):
    folder = tmp_path / "mission_logs"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha")
    (folder / "b.txt").write_text("bravo")
    sub = folder / "nested"
    sub.mkdir()
    (sub / "c.txt").write_text("charlie")

    session = remote_ops.FileTransferSession(str(tmp_path))
    responses = []
    session.handle_file_req({"filename": "mission_logs", "direction": "download"}, responses.append)

    req_msgs = [r["FILE_REQ"] for r in responses if "FILE_REQ" in r]
    chunk_msgs = [r["FILE_CHUNK"] for r in responses if "FILE_CHUNK" in r]
    complete_msgs = [r["FILE_COMPLETE"] for r in responses if "FILE_COMPLETE" in r]

    assert req_msgs[0]["filename"] == "mission_logs.zip"
    assert complete_msgs[0]["status"] == "ok"

    reassembled = b"".join(base64.b64decode(c["data"]) for c in chunk_msgs)
    zip_tmp = tmp_path / "_reassembled.zip"
    zip_tmp.write_bytes(reassembled)
    with zipfile.ZipFile(zip_tmp) as zf:
        names = set(zf.namelist())
        assert "a.txt" in names and "b.txt" in names
        assert any(n.endswith("c.txt") for n in names)
        assert zf.read("a.txt") == b"alpha"


def test_downloading_a_folder_cleans_up_its_temp_zip(tmp_path):
    folder = tmp_path / "small_folder"
    folder.mkdir()
    (folder / "x.txt").write_text("x")
    session = remote_ops.FileTransferSession(str(tmp_path))

    session.handle_file_req({"filename": "small_folder", "direction": "download"}, lambda r: None)

    # No stray xparo_folder_dl_* temp dirs left behind in the system tmp dir.
    import tempfile as _tempfile
    leftovers = [
        name for name in os.listdir(_tempfile.gettempdir())
        if name.startswith("xparo_folder_dl_")
    ]
    assert leftovers == []


# ------------------------------------------------------------------
# Power management -- reboot. Subprocess is mocked here, deliberately --
# the one legitimate exception to this file's "real execution" convention,
# since actually running `sudo reboot` would take down whatever machine
# runs this test suite.
# ------------------------------------------------------------------
def test_handle_reboot_passwordless_success_sends_nothing():
    responses = []
    with patch('xparo.remote_ops.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        remote_ops.handle_reboot(None, responses.append)
    assert responses == []
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["sudo", "-n", "reboot"]


def test_handle_reboot_passwordless_failure_reports_needs_password():
    responses = []
    with patch('xparo.remote_ops.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="sudo: a password is required",
        )
        remote_ops.handle_reboot(None, responses.append)
    result = responses[0]["REBOOT_RESULT"]
    assert result["success"] is False
    assert result["needs_password"] is True


def test_handle_reboot_with_password_pipes_it_via_stdin_not_argv():
    responses = []
    with patch('xparo.remote_ops.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        remote_ops.handle_reboot("hunter2", responses.append)
    args, kwargs = mock_run.call_args
    assert args[0] == ["sudo", "-S", "reboot"]
    assert "hunter2" not in args[0]  # never a CLI arg
    assert kwargs["input"] == "hunter2\n"


def test_handle_reboot_wrong_password_reports_failure():
    responses = []
    with patch('xparo.remote_ops.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Sorry, try again.\nsudo: a password is required",
        )
        remote_ops.handle_reboot("wrong", responses.append)
    result = responses[0]["REBOOT_RESULT"]
    assert result["success"] is False
    assert result["needs_password"] is True


def test_handle_reboot_timeout_reports_failure():
    responses = []
    with patch('xparo.remote_ops.subprocess.run') as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sudo -n reboot", timeout=10.0)
        remote_ops.handle_reboot(None, responses.append)
    assert responses[0]["REBOOT_RESULT"]["success"] is False


# ------------------------------------------------------------------
# ROS2 introspection -- topics (fake node stub, since the interesting
# behavior here is sorting/shaping, not rclpy itself) and parameters
# (real `ros2` CLI subprocess calls -- verified live against an actual
# throwaway rclpy node with declared scalar/list/bool parameters before
# this test was written, confirming ros2 param dump's real output format:
# block-style `- item` lists at the SAME indent as their key, wrapped in
# `<node>: {ros__parameters: {...}}`).
# ------------------------------------------------------------------
class _FakeRos2Node:
    def __init__(self, topics=None, nodes=None):
        self._topics = topics or []
        self._nodes = nodes or []

    def get_topic_names_and_types(self):
        return self._topics

    def get_node_names_and_namespaces(self):
        return self._nodes


def test_handle_list_ros2_topics_sorts_and_shapes():
    node = _FakeRos2Node(topics=[
        ("/zzz_topic", ["std_msgs/msg/String"]),
        ("/aaa_topic", ["std_msgs/msg/Bool", "std_msgs/msg/Empty"]),
    ])
    responses = []
    remote_ops.handle_list_ros2_topics(node, responses.append)
    topics = responses[0]["ROS2_TOPICS"]["topics"]
    assert [t["name"] for t in topics] == ["/aaa_topic", "/zzz_topic"]
    assert topics[0]["types"] == ["std_msgs/msg/Bool", "std_msgs/msg/Empty"]


REAL_PARAM_DUMP_OUTPUT = """/param_dump_test_node:
  ros__parameters:
    my_double: 3.14
    my_int_list:
    - 1
    - 2
    - 3
    my_string: hello world
    my_string_list:
    - a
    - b
    start_type_description_service: true
    use_sim_time: false
"""


def test_parse_param_dump_matches_real_ros2_param_dump_output():
    """REAL_PARAM_DUMP_OUTPUT above is not hypothetical -- it's the exact,
    byte-for-byte output `ros2 param dump` produced against a real rclpy
    node in this environment (ROS2 Jazzy) with those five parameters
    declared, captured before this parser was written this way. An
    earlier version of this parser assumed flow-style `[1, 2, 3]` lists
    and no `<node>/ros__parameters` wrapper -- both wrong, caught by
    actually running the real CLI rather than guessing its format.
    """
    result = dict(remote_ops._parse_param_dump(REAL_PARAM_DUMP_OUTPUT))
    assert result == {
        "my_double": 3.14,
        "my_int_list": [1, 2, 3],
        "my_string": "hello world",
        "my_string_list": ["a", "b"],
        "start_type_description_service": True,
        "use_sim_time": False,
    }


def test_handle_list_ros2_params_real_subprocess_reports_a_clean_error_for_a_missing_node():
    """Real, unmocked `ros2 param dump` call against a node that
    genuinely doesn't exist -- confirmed live to fail fast (~1s, exit
    code 1, "Node not found"), not hang, so this is safe to run for real
    in a test rather than mocking subprocess.run.
    """
    node = _FakeRos2Node(nodes=[("definitely_not_a_real_node_xyz", "/")])
    responses = []
    remote_ops.handle_list_ros2_params(node, responses.append)
    result = responses[0]["ROS2_PARAMS"]
    assert result["params"] == []
    assert len(result["errors"]) == 1
    assert "/definitely_not_a_real_node_xyz" in result["errors"][0]


def test_handle_list_ros2_params_stops_within_its_total_time_budget():
    node = _FakeRos2Node(nodes=[("definitely_not_a_real_node_xyz", "/")])
    responses = []
    with patch('xparo.remote_ops.LIST_PARAMS_TOTAL_BUDGET_SEC', 0.0):
        remote_ops.handle_list_ros2_params(node, responses.append)
    result = responses[0]["ROS2_PARAMS"]
    assert any("budget" in e for e in result["errors"])


def test_handle_set_ros2_param_real_subprocess_reports_failure_for_a_missing_node():
    """Real, unmocked `ros2 param set` call -- same fast-fail reasoning
    as the params-list test above."""
    responses = []
    remote_ops.handle_set_ros2_param(
        "/definitely_not_a_real_node_xyz", "some_param", "5", "req-9", responses.append,
    )
    result = responses[0]["SET_ROS2_PARAM_RESULT"]
    assert result["request_id"] == "req-9"
    assert result["success"] is False


def test_handle_set_ros2_param_never_uses_shell_true():
    """node/param/value all arrive over the network -- argv-list form
    only, confirmed by inspecting the actual subprocess.run call."""
    with patch('xparo.remote_ops.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        remote_ops.handle_set_ros2_param("/n", "p", "v; rm -rf /", "req-1", lambda r: None)
    args, kwargs = mock_run.call_args
    assert isinstance(args[0], list)
    assert kwargs.get("shell") is not True


# ------------------------------------------------------------------
# Rosbag recording control -- thin wrappers around a live RosbagControl,
# stubbed here (spinning up the real rosbag2_recorder service infra is
# out of scope for a unit test; rosbag_control.py's own tests already
# cover the real state machine against real services).
# ------------------------------------------------------------------
class _FakeRosbagControl:
    def __init__(self, state="closed", recorder_alive=True):
        self.state = state
        self.recorder_alive = recorder_alive
        self.start_calls = 0
        self.stop_calls = 0

    def handle_start(self):
        self.start_calls += 1
        self.state = "writing"

    def handle_stop(self, on_done=None):
        self.stop_calls += 1
        self.state = "closed"
        if on_done:
            on_done()


def test_get_rosbag_status_when_unavailable():
    with patch('xparo.rosbag_control.is_any_rosbag_process_running', return_value=False):
        assert remote_ops.get_rosbag_status(None) == {
            "state": "unavailable", "recorder_alive": False, "process_detected": False,
        }


def test_get_rosbag_status_reflects_live_state():
    control = _FakeRosbagControl(state="writing", recorder_alive=True)
    with patch('xparo.rosbag_control.is_any_rosbag_process_running', return_value=True):
        assert remote_ops.get_rosbag_status(control) == {
            "state": "writing", "recorder_alive": True, "process_detected": True,
        }


def test_get_rosbag_status_flags_a_process_recorder_control_cant_identify():
    """The whole point of process_detected: a recorder launched with a
    custom --node-name never answers /rosbag2_recorder's services, so
    RosbagControl itself sees CLOSED/dead -- but a plain process-table
    scan still honestly reports that something is recording."""
    control = _FakeRosbagControl(state="closed", recorder_alive=False)
    with patch('xparo.rosbag_control.is_any_rosbag_process_running', return_value=True):
        status = remote_ops.get_rosbag_status(control)
    assert status["recorder_alive"] is False
    assert status["process_detected"] is True


def test_handle_rosbag_action_start_calls_handle_start():
    control = _FakeRosbagControl(state="closed")
    responses = []
    remote_ops.handle_rosbag_action(control, "start", responses.append)
    assert control.start_calls == 1
    assert responses[0]["ROSBAG_ACTION_RESULT"]["action"] == "start"
    assert responses[0]["ROSBAG_ACTION_RESULT"]["state"] == "writing"


def test_handle_rosbag_action_stop_calls_handle_stop():
    control = _FakeRosbagControl(state="writing")
    responses = []
    remote_ops.handle_rosbag_action(control, "stop", responses.append)
    assert control.stop_calls == 1
    assert responses[0]["ROSBAG_ACTION_RESULT"]["state"] == "closed"


def test_handle_rosbag_action_save_is_an_alias_for_stop():
    """Single-mcap-file mode means save/split has nowhere distinct to go
    (rosbag_control.py's own control_cb hard-disables it) -- Save calls
    the exact same handle_stop() as Stop, just labeled differently."""
    control = _FakeRosbagControl(state="writing")
    responses = []
    remote_ops.handle_rosbag_action(control, "save", responses.append)
    assert control.stop_calls == 1
    assert control.start_calls == 0
    assert responses[0]["ROSBAG_ACTION_RESULT"]["action"] == "save"
    assert responses[0]["ROSBAG_ACTION_RESULT"]["state"] == "closed"


def test_handle_rosbag_action_with_no_live_control_still_replies():
    responses = []
    with patch('xparo.rosbag_control.is_any_rosbag_process_running', return_value=False):
        remote_ops.handle_rosbag_action(None, "start", responses.append)
    assert responses[0]["ROSBAG_ACTION_RESULT"] == {
        "action": "start", "state": "unavailable", "recorder_alive": False, "process_detected": False,
    }


# ------------------------------------------------------------------
# Live system status -- one-shot on-demand snapshot
# ------------------------------------------------------------------
def test_handle_get_live_status_shapes_the_response():
    responses = []
    remote_ops.handle_get_live_status(
        get_resource_consumption=lambda: {
            "cpu_avg_percent": 12.5, "ram_used_percent": 40.0,
            "disk_used_percent": 55.0, "gpu_percent": None,
        },
        get_cpu_temperature=lambda: 47.3,
        uptime_seconds=125.0,
        send_response=responses.append,
    )
    assert responses[0] == {"LIVE_STATUS": {
        "cpu_percent": 12.5, "ram_percent": 40.0, "disk_percent": 55.0,
        "gpu_percent": None, "temp_c": 47.3, "uptime_seconds": 125.0,
    }}
    assert "battery" not in responses[0]["LIVE_STATUS"]


# ------------------------------------------------------------------
# Terminal -- per-request max_lines (previously a fixed module constant)
# ------------------------------------------------------------------
def test_clamp_command_max_lines_defaults_and_bounds():
    assert remote_ops.clamp_command_max_lines(None) == remote_ops.RUN_COMMAND_MAX_LINES
    assert remote_ops.clamp_command_max_lines("not-a-number") == remote_ops.RUN_COMMAND_MAX_LINES
    assert remote_ops.clamp_command_max_lines(0) == 1
    assert remote_ops.clamp_command_max_lines(99999) == remote_ops.MAX_COMMAND_MAX_LINES
    assert remote_ops.clamp_command_max_lines(10) == 10


def test_handle_run_command_respects_a_custom_max_lines():
    cmd = "python3 -c \"[print(i) for i in range(50)]\""
    responses = []
    remote_ops.handle_run_command(cmd, "req-5", 10.0, responses.append, max_lines=5)
    result = responses[0]["COMMAND_RESULT"]
    lines = result["output"].splitlines()
    assert result["truncated"] is True
    assert result["max_lines"] == 5
    assert len(lines) == 5
    assert lines[-1] == "49"


def test_handle_run_command_default_max_lines_unchanged_when_not_specified():
    cmd = "python3 -c \"[print(i) for i in range(200)]\""
    responses = []
    remote_ops.handle_run_command(cmd, "req-6", 10.0, responses.append)
    result = responses[0]["COMMAND_RESULT"]
    assert len(result["output"].splitlines()) == remote_ops.RUN_COMMAND_MAX_LINES
    assert result["max_lines"] == remote_ops.RUN_COMMAND_MAX_LINES
