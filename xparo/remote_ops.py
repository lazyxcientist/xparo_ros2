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
import shutil
import subprocess
import tempfile
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
MAX_COMMAND_MAX_LINES = 1000
DEFAULT_COMMAND_TIMEOUT_SEC = 30.0
MAX_COMMAND_TIMEOUT_SEC = 300.0


def clamp_command_timeout(value):
    try:
        t = float(value)
    except (TypeError, ValueError):
        t = DEFAULT_COMMAND_TIMEOUT_SEC
    return max(1.0, min(t, MAX_COMMAND_TIMEOUT_SEC))


def clamp_command_max_lines(value):
    """Mirrors clamp_command_timeout exactly, for the per-request output
    line cap (jetson_tcp_node_d_m.py's own _clamp_command_max_lines --
    ported here since RUN_COMMAND_MAX_LINES used to be a fixed module
    constant with no way for a caller to ask for more/less)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = RUN_COMMAND_MAX_LINES
    return max(1, min(n, MAX_COMMAND_MAX_LINES))


def handle_run_command(command, request_id, timeout, send_response, max_lines=None):
    """Blocks the calling thread for up to `timeout` seconds -- callers
    that can't afford to block their dispatch loop must run this in its
    own thread (engine.py's on_ws_message does exactly that, one thread
    per command, matching the original).
    """
    max_lines = clamp_command_max_lines(max_lines) if max_lines is not None else RUN_COMMAND_MAX_LINES
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
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[-max_lines:]

    send_response({"COMMAND_RESULT": {
        "request_id": request_id,
        "command": command,
        "success": (exit_code == 0) and not timed_out,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output": "\n".join(lines),
        "truncated": truncated,
        "max_lines": max_lines,
    }})


# ----------------------------------------------------------------------
# Power management -- reboot
#
# Ported from jetson_tcp_node_d_m.py's _do_reboot: try passwordless sudo
# first (fails fast, no hang on a TTY prompt); if a password was supplied,
# retry with it piped via stdin (never a CLI arg -- would leak into `ps`).
# On success there is nothing to send back (the machine is going down);
# only a failure gets a response, so the caller knows to prompt for a
# password (or that the one given was wrong).
# ----------------------------------------------------------------------
REBOOT_TIMEOUT_SEC = 10.0


def handle_reboot(password, send_response):
    try:
        if not password:
            result = subprocess.run(
                ["sudo", "-n", "reboot"], capture_output=True, text=True, timeout=REBOOT_TIMEOUT_SEC,
            )
        else:
            result = subprocess.run(
                ["sudo", "-S", "reboot"], input=password + "\n",
                capture_output=True, text=True, timeout=REBOOT_TIMEOUT_SEC,
            )
        password = None  # never kept around a moment longer than needed
        if result.returncode == 0:
            return  # success -- the machine is rebooting, nothing more to say
        message = (result.stdout or "") + (result.stderr or "")
        send_response({"REBOOT_RESULT": {
            "success": False, "message": message.strip(),
            "needs_password": "password" in message.lower(),
        }})
    except subprocess.TimeoutExpired:
        send_response({"REBOOT_RESULT": {
            "success": False, "message": f"reboot command timed out after {REBOOT_TIMEOUT_SEC:.0f}s",
            "needs_password": False,
        }})
    except Exception as e:
        send_response({"REBOOT_RESULT": {"success": False, "message": str(e), "needs_password": False}})


# ----------------------------------------------------------------------
# ROS2 introspection -- topics (live rclpy graph query) and parameters
# (ros2 CLI subprocess, deliberately: no rclpy parameter-client/executor
# wiring needed, and a hung `ros2` invocation can't block this node --
# same rationale jetson_tcp_node_d_m.py documents for its own choice).
# ----------------------------------------------------------------------
PARAM_DUMP_TIMEOUT_SEC = 5.0
LIST_PARAMS_TOTAL_BUDGET_SEC = 45.0
SET_PARAM_TIMEOUT_SEC = 10.0


def handle_list_ros2_topics(node, send_response):
    topics = [
        {"name": name, "types": types}
        for name, types in sorted(node.get_topic_names_and_types())
    ]
    send_response({"ROS2_TOPICS": {"topics": topics}})


def _parse_param_dump(yaml_ish_text):
    """Hand-rolled indentation parser for `ros2 param dump`'s output --
    deliberately avoids a PyYAML dependency. NOT a straight port of
    jetson_tcp_node_d_m.py's own parser -- confirmed live, against a real
    node's real `ros2 param dump` output (ROS2 Jazzy), that this format
    differs from what that parser assumed in two ways: (1) the whole
    thing is wrapped in `<node_name>:\\n  ros__parameters:\\n    ...`, and
    (2) a list value is block-style YAML --

        my_int_list:
        - 1
        - 2

    with each `- item` line at the SAME indentation as its own `key:`
    line, not indented further -- a bare "pop the stack once indent <=
    the current top's indent" rule (which is otherwise correct for
    nested mappings) would misread every `- item` line as a sibling key
    of the list itself rather than one of its elements. `- ` lines are
    therefore handled as their own case, appended to whatever key was
    most recently opened with an empty value, without ever touching the
    indent-stack pop logic non-list lines use.
    """
    root = {}
    stack = [(-1, root)]
    pending_list_key = None  # (parent_dict, key) -- where to append `- item` lines

    for raw_line in yaml_ish_text.splitlines():
        if not raw_line.strip() or raw_line.strip().startswith('#'):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(' '))
        line = raw_line.strip()

        if line.startswith('- '):
            if pending_list_key is not None:
                parent, key = pending_list_key
                if not isinstance(parent.get(key), list):
                    parent[key] = []
                parent[key].append(_coerce_param_value(line[2:]))
            continue

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        pending_list_key = None

        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        value = value.strip()
        if value == '':
            # Ambiguous until the next line arrives: a nested mapping
            # (deeper indent follows) or a block list (`- item` at this
            # SAME indent follows). Both cases are covered: a nested
            # mapping recurses normally through the stack; a list
            # instead gets caught by pending_list_key above and
            # overwrites this same dict with a list once its first
            # `- item` line is seen.
            child = {}
            parent[key] = child
            stack.append((indent, child))
            pending_list_key = (parent, key)
        else:
            parent[key] = _coerce_param_value(value)

    # Unwrap `<node_name>: {ros__parameters: {...}}` -- the real params
    # live two levels below the single top-level node-name key, which
    # would otherwise show up baked into every flattened name.
    top_level = next(iter(root.values()), {})
    params_root = top_level.get('ros__parameters', top_level) if isinstance(top_level, dict) else {}

    def _flatten(prefix, node, out):
        for key, value in node.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                _flatten(full_key, value, out)
            else:
                out.append((full_key, value))

    flat = []
    _flatten('', params_root, flat)
    return flat


def _coerce_param_value(value):
    value = value.strip()
    if value.startswith('[') and value.endswith(']'):
        # Flow-style `[a, b]` never actually comes out of `ros2 param
        # dump` (it always uses block-style `- item` lines, see
        # _parse_param_dump above) -- kept only as a harmless, cheap
        # fallback in case a value's own string content legitimately
        # looks like this.
        inner = value[1:-1].strip()
        return [_coerce_param_value(v) for v in inner.split(',')] if inner else []
    if value in ('true', 'True'):
        return True
    if value in ('false', 'False'):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value.strip('"\'')


def handle_list_ros2_params(node, send_response):
    """Runs in its own thread (see engine.py's dispatch) -- bounded by
    LIST_PARAMS_TOTAL_BUDGET_SEC across every node combined, so a stuck
    `ros2 param dump` on one node can't block the rest forever; whatever
    was gathered before the budget ran out is still returned, plus an
    error note, rather than the caller timing out with nothing at all.
    """
    import time as _time
    started = _time.monotonic()
    params = []
    errors = []
    try:
        node_names = sorted(
            (ns.rstrip('/') + '/' + name if ns not in ('', '/') else '/' + name)
            for name, ns in node.get_node_names_and_namespaces()
        )
    except Exception as e:
        send_response({"ROS2_PARAMS": {"params": [], "errors": [f"could not list nodes: {e}"]}})
        return

    for full_node_name in node_names:
        if _time.monotonic() - started > LIST_PARAMS_TOTAL_BUDGET_SEC:
            errors.append(f"stopped early: exceeded {LIST_PARAMS_TOTAL_BUDGET_SEC:.0f}s total budget")
            break
        try:
            result = subprocess.run(
                ["ros2", "param", "dump", full_node_name],
                capture_output=True, text=True, timeout=PARAM_DUMP_TIMEOUT_SEC,
            )
            if result.returncode != 0:
                errors.append(f"{full_node_name}: {(result.stderr or 'dump failed').strip()}")
                continue
            for name, value in _parse_param_dump(result.stdout):
                params.append({"node": full_node_name, "name": name, "value": value})
        except subprocess.TimeoutExpired:
            errors.append(f"{full_node_name}: dump timed out")
        except Exception as e:
            errors.append(f"{full_node_name}: {e}")

    send_response({"ROS2_PARAMS": {"params": params, "errors": errors}})


def handle_set_ros2_param(node_name, param_name, value, request_id, send_response):
    """Runs `ros2 param set` as an argv list, deliberately NOT shell=True
    -- node_name/param_name/value all arrive over the network."""
    try:
        result = subprocess.run(
            ["ros2", "param", "set", node_name, param_name, str(value)],
            capture_output=True, text=True, timeout=SET_PARAM_TIMEOUT_SEC,
        )
        success = result.returncode == 0
        message = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        success = False
        message = f"set_param timed out after {SET_PARAM_TIMEOUT_SEC:.0f}s"
    except Exception as e:
        success = False
        message = str(e)
    send_response({"SET_ROS2_PARAM_RESULT": {
        "request_id": request_id, "node": node_name, "name": param_name,
        "success": success, "message": message.strip(),
    }})


# ----------------------------------------------------------------------
# Rosbag recording control -- thin wrappers around a live RosbagControl
# instance (see rosbag_control.py), which already owns all the real
# service-call/state-machine logic. "Save" is deliberately an alias for
# Stop: this project's rosbag setup runs single-mcap-file mode, so
# save/split has nowhere distinct to go (rosbag_control.py's own
# control_cb already hard-disables it for the same reason) -- Stop
# already cleanly finalizes the current bag, which is what "save" means
# here.
# ----------------------------------------------------------------------
def get_rosbag_status(rosbag_control):
    """process_detected is independent of state/recorder_alive (which come
    from RosbagControl's own /rosbag2_recorder service-based state
    machine, matching whatever node currently answers that name) -- a
    plain process-table scan (rosbag_control.py's
    is_any_rosbag_process_running), so a `ros2 bag record` launched with a
    custom --node-name (the one case the shared-default-name detection
    genuinely can't identify or control) still shows up as "something is
    recording", honestly, even though state/recorder_alive can't reflect
    it.
    """
    from .rosbag_control import is_any_rosbag_process_running
    process_detected = is_any_rosbag_process_running()
    if rosbag_control is None:
        return {"state": "unavailable", "recorder_alive": False, "process_detected": process_detected}
    return {
        "state": rosbag_control.state, "recorder_alive": rosbag_control.recorder_alive,
        "process_detected": process_detected,
    }


def handle_rosbag_action(rosbag_control, action, send_response):
    if rosbag_control is not None:
        if action == "start":
            rosbag_control.handle_start()
        elif action in ("stop", "save"):
            rosbag_control.handle_stop()
    send_response({"ROSBAG_ACTION_RESULT": {"action": action, **get_rosbag_status(rosbag_control)}})


# ----------------------------------------------------------------------
# Live system status -- one-shot on-demand snapshot (no streaming/gating
# needed: the popup fetches this once on open plus a manual refresh, per
# the user's own "show current cpu/ram/storage on OPENING the popup"
# request). No battery field -- no battery telemetry subsystem exists in
# this repo yet (see bt_engine/nodes/check_battery_level.py's own stub
# docstring), and this repo's convention is to omit a reading entirely
# rather than fake one, not to fabricate a number.
# ----------------------------------------------------------------------
def handle_get_live_status(get_resource_consumption, get_cpu_temperature, uptime_seconds, send_response):
    resources = get_resource_consumption()
    send_response({"LIVE_STATUS": {
        "cpu_percent": resources.get("cpu_avg_percent"),
        "ram_percent": resources.get("ram_used_percent"),
        "disk_percent": resources.get("disk_used_percent"),
        "gpu_percent": resources.get("gpu_percent"),
        "temp_c": get_cpu_temperature(),
        "uptime_seconds": uptime_seconds,
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

                # A folder is zipped server-side first, then streamed
                # through the exact same chunked sequence as a single file
                # -- the wire protocol doesn't need to know the difference
                # (mirrors jetson_tcp_node_d_m.py's own download_folder,
                # which reuses its single-file download path the same way
                # once the zip exists). The temp zip is always cleaned up,
                # success or failure.
                zip_tmp_dir = None
                if file_path.is_dir():
                    zip_tmp_dir = tempfile.mkdtemp(prefix="xparo_folder_dl_")
                    archive_base = os.path.join(zip_tmp_dir, file_path.name)
                    zip_path = shutil.make_archive(archive_base, "zip", root_dir=str(file_path))
                    stream_path = Path(zip_path)
                    download_name = file_path.name + ".zip"
                else:
                    stream_path = file_path
                    download_name = Path(rel).name

                try:
                    file_size = stream_path.stat().st_size
                    send_response({"FILE_REQ": {
                        "filename": download_name, "size": file_size, "direction": "download",
                    }})
                    with open(stream_path, "rb") as f:
                        while True:
                            chunk = f.read(FILE_CHUNK_SIZE)
                            if not chunk:
                                break
                            send_response({"FILE_CHUNK": {"data": base64.b64encode(chunk).decode("ascii")}})
                    send_response({"FILE_COMPLETE": {"status": "ok", "expected": file_size, "received": file_size}})
                finally:
                    if zip_tmp_dir is not None:
                        shutil.rmtree(zip_tmp_dir, ignore_errors=True)
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
