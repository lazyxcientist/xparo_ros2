"""Transport-agnostic exec/file-transfer/teleop handler bodies -- ported
from tethered_module's jetson_tcp_node_d_m.py (the ROV-specific sensor/
telemetry/failsafe logic in that file is NOT ported here; it's specific to
that particular vehicle's hardware, not a general xparo fleet capability --
only the three features the fleet-unification plan actually asked for are).

Every handler here takes a send_response(dict) callback rather than writing
to a socket/channel-group directly, so engine.py's dispatch table can wire
the exact same functions to either Transport (django_ws or tethered_tcp,
see transports/base.py) without caring which one delivered the message --
that's the actual point of Phase 2's Transport abstraction.
"""
import base64
import os
import subprocess
from pathlib import Path

# ----------------------------------------------------------------------
# Remote Terminal ("RUN_COMMAND")
#
# NOTE ON TRUST MODEL: matches jetson_tcp_node_d_m.py's own -- runs
# whatever shell command arrives with no additional authentication beyond
# whatever already gated the connection itself (a per-robot RobotCredential
# for django_ws; the physical tether for tethered_tcp -- there is no
# credential concept there at all, same as the original). Each command
# runs as its own OS subprocess (never inline in this thread), so a hung
# or crashing command can never take down the transport or the rest of
# this node -- it just times out.
# ----------------------------------------------------------------------
RUN_COMMAND_MAX_LINES = 50
DEFAULT_COMMAND_TIMEOUT_SEC = 30.0
MAX_COMMAND_TIMEOUT_SEC = 300.0


def clamp_command_timeout(value):
    try:
        t = float(value)
    except (TypeError, ValueError):
        t = DEFAULT_COMMAND_TIMEOUT_SEC
    return max(1.0, min(t, MAX_COMMAND_TIMEOUT_SEC))


def handle_run_command(command, request_id, timeout, send_response):
    """Blocks the calling thread for up to `timeout` seconds -- callers
    that can't afford to block their dispatch loop must run this in its
    own thread (engine.py's on_ws_message does exactly that, one thread
    per command, matching the original).
    """
    timed_out = False
    exit_code = None
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        exit_code = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        output = f"{stdout}{stderr}\n[killed - exceeded {timeout:.0f}s timeout]"
    except Exception as e:
        output = f"Failed to run command: {e}"

    lines = output.splitlines()
    truncated = len(lines) > RUN_COMMAND_MAX_LINES
    if truncated:
        lines = lines[-RUN_COMMAND_MAX_LINES:]

    send_response({"COMMAND_RESULT": {
        "request_id": request_id,
        "command": command,
        "success": (exit_code == 0) and not timed_out,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output": "\n".join(lines),
        "truncated": truncated,
    }})


# ----------------------------------------------------------------------
# Teleop
# ----------------------------------------------------------------------
# core's fin/thruster controller indexes msg.axes[0..3] and
# msg.buttons[0..2] directly (see jetson_tcp_node_d_m.py's own comment on
# this) -- a short/empty payload (e.g. a virtual joystick sending
# buttons:[]) reaching it as-is throws an uncaught IndexError there. Pad
# every teleop message up to this minimum length so no client can trigger
# that.
MIN_JOY_AXES = 4
MIN_JOY_BUTTONS = 3


def handle_teleop(axes, buttons, joy_publish, send_response):
    """joy_publish(axes: list[float], buttons: list[int]) -> None -- the
    caller supplies this (wired to a real rclpy Publisher in production,
    a plain recorder in tests) since publishing a sensor_msgs/Joy message
    needs an actual ROS2 node context this module deliberately doesn't
    depend on.
    """
    axes = [float(a) for a in (axes or [])]
    buttons = [int(bool(b)) for b in (buttons or [])]
    if len(axes) < MIN_JOY_AXES:
        axes += [0.0] * (MIN_JOY_AXES - len(axes))
    if len(buttons) < MIN_JOY_BUTTONS:
        buttons += [0] * (MIN_JOY_BUTTONS - len(buttons))
    joy_publish(axes, buttons)
    send_response({"TELEOP_ACK": {"success": True}})


# ----------------------------------------------------------------------
# File listing / deletion
# ----------------------------------------------------------------------
def list_files(base_dir):
    """Tree under base_dir. Each entry: {"type": "folder"|"file", "name":
    str, "path": str (relative to base_dir), "size": int (files only),
    "children": [...] (folders only)}.
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    def walk(directory, prefix=""):
        entries = []
        try:
            items = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return entries
        for item in items:
            rel = (prefix + "/" + item.name).lstrip("/")
            if item.is_dir():
                entries.append({
                    "type": "folder", "name": item.name, "path": rel,
                    "children": walk(item, rel),
                })
            else:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                entries.append({"type": "file", "name": item.name, "path": rel, "size": size})
        return entries

    return walk(base)


def handle_list_files(base_dir, send_response):
    # base_dir surfaced alongside the tree so the file browser can show
    # exactly where on the robot's filesystem these files actually live
    # (e.g. for finding them again over a Terminal/SSH session) -- without
    # this, "transferred_files" is a name with no path, and every robot's
    # is a different absolute location (ros_packages/.../transferred_files
    # under wherever that particular checkout lives).
    send_response({"FILE_LIST": {"tree": list_files(base_dir), "base_dir": str(Path(base_dir).resolve())}})


def handle_delete_file(base_dir, rel_path, send_response):
    """rel_path is untrusted input from the wire -- resolve() + a
    startswith(base + os.sep) check is the path-traversal guard (the
    original _delete_jetson_file this was ported from used a bare
    startswith(base), which wrongly accepts a sibling directory whose name
    happens to share base's string as a prefix, e.g. base="/x/data" would
    let rel_path="../data_evil/f" through -- not reachable against this
    package's fixed transfer_dir layout today, but cheap to close anyway).
    """
    base = Path(base_dir).resolve()
    try:
        target = (base / rel_path).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            send_response({"DELETE_ACK": {"success": False, "message": "Path traversal denied"}})
            return
        if not target.exists():
            send_response({"DELETE_ACK": {"success": False, "message": "File not found"}})
            return
        if target.is_dir():
            send_response({"DELETE_ACK": {"success": False, "message": "Cannot delete a directory"}})
            return
        target.unlink()
        send_response({"DELETE_ACK": {"success": True, "path": rel_path}})
    except Exception as e:
        send_response({"DELETE_ACK": {"success": False, "message": str(e)}})


# ----------------------------------------------------------------------
# File transfer (upload/download) -- base64-in-JSON adaptation of
# jetson_tcp_node_d_m.py's FileTransferHandler, which framed raw binary
# directly over TCP. django_ws rides AsyncJsonWebsocketConsumer (JSON text
# frames only), so chunks travel as base64 strings inside FILE_CHUNK
# messages instead of raw bytes; tethered_tcp can still carry them as raw
# binary at the wire level (see transports/tethered_tcp.py) since the
# framing there is independent of this class either way -- this class only
# ever sees already-decoded dicts, via send_response/handle_file_chunk.
# ----------------------------------------------------------------------
FILE_CHUNK_SIZE = 65536


class FileTransferSession:
    """One long-lived instance per Engine (not per-message) -- only one
    upload is tracked at a time, matching the original's single-channel
    behavior and its documented reason (a second concurrent consumer of
    transfer state was the root cause of duplicate acks/corrupted
    decoding in an earlier version of the code this was ported from).
    """

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._current_upload = None

    def handle_file_req(self, req, send_response):
        try:
            filename = Path(req.get("filename", "")).name
            direction = req.get("direction", "upload")

            if direction == "upload":
                # A FILE_REQ(upload) while a previous one is still open
                # (abandoned mid-transfer, never completed) must not just
                # overwrite _current_upload -- that would leak the old
                # file handle. Close it out first, same as _abort_upload.
                self._abort_upload()
                size = req.get("size", 0)
                dest_path = self.base_dir / filename
                try:
                    f = open(dest_path, "wb")
                except OSError as e:
                    send_response({"error": {"message": str(e)}})
                    return
                self._current_upload = {"file": f, "expected_size": size, "received": 0}
                send_response({"FILE_REQ": {"filename": filename, "direction": "upload", "ready": True}})

            elif direction == "download":
                rel = req.get("filename", "")
                transfer_base = self.base_dir.resolve()
                file_path = (self.base_dir / rel).resolve()
                if file_path != transfer_base and not str(file_path).startswith(str(transfer_base) + os.sep):
                    send_response({"error": {"message": "Path traversal denied"}})
                    return
                if not file_path.exists():
                    send_response({"error": {"message": "File not found"}})
                    return
                file_size = file_path.stat().st_size
                send_response({"FILE_REQ": {
                    "filename": Path(rel).name, "size": file_size, "direction": "download",
                }})
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(FILE_CHUNK_SIZE)
                        if not chunk:
                            break
                        send_response({"FILE_CHUNK": {"data": base64.b64encode(chunk).decode("ascii")}})
                send_response({"FILE_COMPLETE": {"status": "ok", "expected": file_size, "received": file_size}})
        except Exception as e:
            send_response({"error": {"message": str(e)}})

    def handle_file_chunk(self, chunk_payload):
        if not self._current_upload:
            return
        try:
            data = base64.b64decode(chunk_payload.get("data", ""))
            self._current_upload["file"].write(data)
            self._current_upload["received"] += len(data)
        except Exception:
            self._abort_upload()

    def handle_file_complete(self, send_response):
        if not self._current_upload:
            return
        expected = self._current_upload["expected_size"]
        received = self._current_upload["received"]
        self._current_upload["file"].close()
        self._current_upload = None
        send_response({"FILE_COMPLETE": {"status": "ok", "expected": expected, "received": received}})

    def _abort_upload(self):
        if self._current_upload:
            self._current_upload["file"].close()
            self._current_upload = None
