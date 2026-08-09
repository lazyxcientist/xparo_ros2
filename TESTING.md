# Testing the XPARO ROS API

This is the practical guide to exercising *every* capability the `xparo`
ROS2 package exposes over its dispatch-key API -- what to send, what comes
back, and three different ways to send it (dashboard UI, raw WebSocket,
`ros2` CLI), depending on whether you're testing the whole stack or just
the robot side in isolation.

For the automated `pytest` suite (fast, no live server/robot needed), see
[`test/README.md`](test/README.md). This document is
the complement to that one: **live**, end-to-end capability testing
against a real (or locally-run) robot process and Django server.

For "how do I install and launch this package at all", see
[`README.md`](README.md). This document assumes
you've already got a node running (or are about to, per the Quick Start
below) and want to poke at what it can do.

## Contents

- [Quick start: a local robot + local Django server](#quick-start-a-local-robot--local-django-server)
- [How messages actually travel](#how-messages-actually-travel)
- [The dispatch-key API, capability by capability](#the-dispatch-key-api-capability-by-capability)
- [Three ways to test each capability](#three-ways-to-test-each-capability)
- [Auth: project secret vs. per-robot credential](#auth-project-secret-vs-per-robot-credential)
- [Rosbag recording](#rosbag-recording)
- [Tethered transport (`tethered_tcp`)](#tethered-transport-tethered_tcp)
- [Troubleshooting](#troubleshooting)

---

## Quick start: a local robot + local Django server

You don't need a physical robot to test the API -- `Engine` is plain
Python and runs fine on a laptop against a local Django dev server.

**1. Start Django** (from the repo root, not `ros_packages/`):

```bash
python manage.py runserver 0.0.0.0:8000
# or: daphne -b 0.0.0.0 -p 8000 core.asgi:application
```

**2. Create a project and grab its secret** through the dashboard UI --
sign up, create a project, open its settings and issue a secret key
there (`apps/DASH_app/api_keys.py`'s `secret_keys` POST handler; the raw
value is shown exactly once). Or, from `manage.py shell`, using the same
hash-and-store pattern that view uses:

```python
import secrets, hashlib
from apps.DASH_app.models import Project_Dashboard, ProjectSecretKey

project = Project_Dashboard.objects.create(title="local-test", disc="")
raw_secret = secrets.token_hex(32)
ProjectSecretKey.objects.create(
    project=project, key_hash=hashlib.sha256(raw_secret.encode()).hexdigest(),
    prefix=raw_secret[:8],
)
print(raw_secret, project.id)
```

**3. Run the node against it, pointed at `local`** (this is what
`xparo_environment` is for -- see `transports/django_ws.py`'s
`DEFAULT_ENVIRONMENT`; without it the node targets production
`xparo.in`):

```bash
cd ros_packages
colcon build --symlink-install
source install/setup.bash
ros2 run xparo xparo_ros --ros-args \
  -p xparo_project_id:="<project_id>" \
  -p xparo_secret_key:="<raw_secret>" \
  -p xparo_environment:="local"
```

You should see the XPARO banner and `\\Connection Sussessfull//` in the
node's stdout, and the robot appear in the dashboard's Robots Fleet page
within a few seconds. From here, every capability below is reachable
either through that dashboard page or directly over the socket.

---

## How messages actually travel

```
Browser (React)  <—— ws/dash_app/<user>/<project>/ ——>  Django (Manage_Dash.py)
                                                              |
                                                     group_send('robot'+id)
                                                              |
Robot (xparo_ros) <—— ws/chatbot_api/<secret>/<project>/ ——> Django (consumers_API.py)
```

Every message, in either direction, is a single JSON object whose
**one** top-level key names the capability (`{"RUN_COMMAND": {...}}`,
`{"TELEOP": {...}}`, ...). On the robot side, `xparo/engine.py`'s
`Engine.on_ws_message` is the entire dispatch table -- every capability
below corresponds to exactly one `elif k == "..."` branch there. On the
Django side, `Manage_Dash.py`'s `send_to_robot(..., raw=True)` is what
gets a browser-originated command to a specific robot's group without an
extra wrapping layer; robot-originated messages reach browsers via
`send_to_ui`, unwrapped the same way on the frontend
(`frontend/src/pages/chats/index.tsx`'s response handler reads `data[key]`
directly).

`remote_ops.py`'s handlers (`RUN_COMMAND`, `TELEOP`, `LIST_FILES`,
`DELETE_FILE`, file transfer) are the one part of this table that's
**transport-agnostic on purpose** -- they take a plain
`send_response(dict)` callback, so the exact same function bodies run
whether the message arrived over `django_ws` or `tethered_tcp` (see
`transports/base.py`'s `Transport` ABC docstring). Everything else in
`on_ws_message` (config sync, credential issuance, heartbeat) is
`django_ws`-specific, since a tethered ROV has no Django to sync config
from at all.

---

## The dispatch-key API, capability by capability

### Robot control (dashboard -> robot)

| Key | Payload | What happens | Response |
|---|---|---|---|
| `RUN_COMMAND` | `{robot_id, command, request_id, timeout?}` | Runs `command` in a shell subprocess on the robot (own thread per command; a hang or crash can't take the node down). `timeout` clamps to 1-300s (`remote_ops.MAX_COMMAND_TIMEOUT_SEC`), default 30s. Output is tail-truncated to the last 50 lines. | `COMMAND_RESULT` |
| `TELEOP` | `{robot_id, axes: [float...], buttons: [0/1...]}` | Publishes a `sensor_msgs/Joy` to **`/joy`**. Short lists are zero-padded up to `MIN_JOY_AXES=4` / `MIN_JOY_BUTTONS=3` server-side (`remote_ops.py`) so a short payload can never crash a downstream controller indexing `axes[0..3]`/`buttons[0..2]`. | `TELEOP_ACK` |
| `LIST_FILES` | `{robot_id}` | Recursively lists `ros_packages/src/xparo/transferred_files/` (the same directory uploads/downloads use). | `FILE_LIST` (`tree`: nested file/folder objects with `size`) |
| `DELETE_FILE` | `{robot_id, path}` | Deletes one file under `transferred_files/`. Path-traversal-guarded (resolves + checks the result is still inside the base dir). | `DELETE_ACK` |
| `FILE_REQ` | `{robot_id, filename, direction: "upload"\|"download", size?}` | Starts a transfer. `upload`: robot opens the destination file and acks `ready:true`. `download`: robot streams the file back as base64 `FILE_CHUNK`s. | `FILE_REQ` (echoed with `ready`/`size`), then chunks |
| `FILE_CHUNK` | `{robot_id, data}` (base64) | One 64KiB-or-less chunk of an in-progress upload. | — |
| `FILE_COMPLETE` | `{robot_id}` | Closes the upload file handle. | `FILE_COMPLETE` (`expected`/`received` byte counts) |

### Robot -> dashboard (telemetry / relay)

| Key | What it carries |
|---|---|
| `ADD_robots_info` | Full hardware/OS/peripherals snapshot, sent on every `on_ws_open`. First-ever connection for a `device_id` mints and returns a one-time `ROBOT_CREDENTIAL`. |
| `ROBOT_HEARTBEAT` | `{device_id}`, sent every 30s (`engine.py`'s `HEARTBEAT_INTERVAL_SECONDS`) independent of any logging session -- this is what `Robots.is_online` actually tracks (90s staleness threshold, so one missed beat is tolerated). |
| `COMMAND_RESULT` | Reply to `RUN_COMMAND` -- `{request_id, command, success, exit_code, timed_out, output, truncated}`. Also closes out the `RemoteCommandLog` row Django opened when the command was dispatched. |
| `GET_live_update_bt` / `ADD_live_update_database` | Behaviour-tree node status stream, relayed from `/bt_xparo_log`. |
| `log_updated`, `update_logging_session` | Logging-session bookkeeping (unrelated to rosbag; this is the AIML/task-history database). |

### Config sync (bidirectional, on connect / on change)

`aiml`, `maps`, `local_env`, `Sets`, `properties`, `custom_aiml`,
`custom_maps`, `custom_Sets`/`custom_sets` -- the robot's behavior tree,
environment blackboard, AIML sets, and hardware properties, pushed from
Django and written to the corresponding path in `self.files` (see
`engine.py`'s `__init__`). `get_initial_local_env_data` /
`sync_local_database` request these explicitly (sent automatically the
first time a `device_id` connects, or when the project only has 1-2
robots).

### Auth / plumbing

| Key | Purpose |
|---|---|
| `ROBOT_CREDENTIAL` | One-time issuance of this robot's per-device credential (see [Auth](#auth-project-secret-vs-per-robot-credential) below); persisted to `config/credential.json` and used instead of `xparo_secret_key` on every reconnect after that. |
| `REST_API_TOKEN` | Arms `XP_Database`'s `BlackboxOrchestrator` with a token for rosbag cloud uploads and flushes anything queued while it was missing. |

---

## Three ways to test each capability

### A. Through the dashboard UI (the real path)

This is what an actual operator does, and the best way to test the
**whole** stack (Django group-targeting, role enforcement, frontend
rendering) at once, not just the robot's handler logic.

1. Log in, open a project, go to **Robots Fleet**, select your robot.
2. Click **"Show more details"** to expand the panel, then the **Remote
   Access** buttons:
   - **Terminal** -> `RUN_COMMAND`/`COMMAND_RESULT`, one request per line, timeout-aware.
   - **Files** -> `LIST_FILES`/`DELETE_FILE`/upload (`FILE_REQ`+`FILE_CHUNK`+`FILE_COMPLETE`)/download, with a real file-tree browser.
   - **Teleop** -> a tabbed popup: **Gamepad** (drives from a real physical controller plugged into your machine via the browser's Gamepad API), **Mapping** (a live gamepad tester -- every connected controller's axes/buttons, for identifying indices before trusting a real drive), and **Raw** (hand-typed axes/buttons lists, one-shot or repeating) -- all three publish the same `TELEOP` key, which reaches `/joy` on the robot.
3. Confirm on the robot side: `ros2 topic echo /joy` while driving from any Teleop tab should show live `Joy` messages.

This is also the path that exercises `VIEWER_BLOCKED_KEYS` (a project
Viewer's `RUN_COMMAND`/`TELEOP`/`DELETE_FILE`/file-transfer attempts are
silently dropped server-side, `LIST_FILES` is not) -- log in as a Viewer
to confirm that boundary holds.

### B. Raw WebSocket (no frontend at all)

Useful for testing the API surface directly, or for reproducing a bug
without the UI in the loop. From a browser's dashboard session (so you
have a valid session cookie), or with `wscat`/`websocket-client` against
`ws/dash_app/<username>/<project_id>/`, send:

```json
{"p": {"<project_id>": {"RUN_COMMAND": {"robot_id": "<robot_id>", "command": "echo hi", "request_id": "test-1"}}}}
```

(`Manage_Dash.py`'s browser-facing consumer expects the `{"p": {project_id:
{...}}}` envelope; the robot-facing side does not -- see the next
section.)

To talk to the **robot's own socket directly** (bypassing Django's
group-targeting entirely -- good for isolating "is this a robot-side bug
or a Django-side bug"), connect to
`ws://127.0.0.1:8000/ws/chatbot_api/<robot's raw secret or credential>/<project_id>/`
and send dispatch-key messages unwrapped:

```python
import json, websocket  # websocket-client -- already a dependency, see requirements.txt

ws = websocket.create_connection("ws://127.0.0.1:8000/ws/chatbot_api/<secret>/<project_id>/")
ws.send(json.dumps({"LIST_FILES": {}}))
print(ws.recv())
```

Since this connects as if *you* were the robot, anything you send here is
routed to real browsers watching the project, not back to a robot -- use
it to test what a robot *sends*, not what it *receives*. To test what a
robot receives without a physical/simulated robot at all, drive
`xparo.engine.Engine` directly in a Python shell:

```python
import sys; sys.path.insert(0, "ros_packages/src/xparo")
from xparo.engine import Engine

responses = []
e = Engine("secret", "project-id", connection_type="offline")
e._send_dict = responses.append  # capture instead of transmitting
e.on_ws_message(None, {"TELEOP": {"axes": [0.5, 0, 0, 0], "buttons": []}})
print(responses)  # [{'TELEOP_ACK': {'success': True}}]
```

This is exactly what
[`test_transports.py`](test/test_transports.py) and the
cross-repo test in the Django suite
(`apps/DASH_app/tests/test_remote_ops_dispatch.py`'s
`test_wire_output_actually_dispatches_in_the_real_robot_engine`) do --
feed real or synthetic wire messages into a real `Engine` and assert on
what comes back, with no network involved.

### C. `ros2` CLI (robot-side only, no Django involved)

For confirming the ROS2-native side of a capability independent of
whether Django/the dashboard is even reachable:

```bash
# Confirm /joy is actually being published (any TELEOP source: dashboard,
# raw WebSocket, or a real physical joystick through the Gamepad tab).
ros2 topic echo /joy

# The legacy Q&A topics (unrelated to Phase 4 remote-ops, still live):
ros2 topic pub /xparo/ask std_msgs/msg/String "data: 'your question here'"
ros2 topic echo /xparo/response

# Behaviour-tree status relay (fed from Nav2's /behavior_tree_log):
ros2 topic echo /bt_xparo_log
```

---

## Auth: project secret vs. per-robot credential

Two credential types both authenticate against
`ws/chatbot_api/<secret>/<project_id>/` (`Manage_Dash.py`'s
`authenticate_user()` tries a `RobotCredential` hash match first, then
falls back to `ProjectSecretKey`):

- **Project secret** (`xparo_secret_key` launch param) -- the legacy,
  project-wide credential. Any robot holding it can identify as *any*
  `device_id` in the project via `ADD_robots_info`. Still fully
  supported; nothing breaks if a robot never gets a per-robot credential.
- **Per-robot credential** (`RobotCredential`, `apps/analytics/robot_auth.py`)
  -- issued automatically, once, the first time a `device_id` connects
  under a project secret (see `ADD_robots_info` -> `ROBOT_CREDENTIAL` in
  the API table above). The robot persists it to `config/credential.json`
  and uses it instead of `xparo_secret_key` on every subsequent
  reconnect. Once a connection resolves a specific `self.robot` this way,
  `device_id` in its payloads is *validated* against that identity rather
  than trusted outright -- this is what closes the "any project-secret
  holder can claim to be any robot" gap.

To test the upgrade path yourself: connect a fresh robot (new
`device_id`) with just `xparo_secret_key` set, confirm `ROBOT_CREDENTIAL`
arrives and `config/credential.json` is written, then kill and restart
the node with the *same* project but no `xparo_secret_key` at all -- it
should still connect, using the persisted credential
(`engine.py`'s `_load_persisted_credential`).

Role enforcement (`ProjectMembership.Role`) is Django-side only, in
`Manage_Dash.py`'s `VIEWER_BLOCKED_KEYS` -- test it from the dashboard UI
(section A above) as a Viewer-role user, not from the robot side (the
robot has no concept of dashboard user roles at all).

---

## Rosbag recording

Recording is controlled at **launch time**, not via a dispatch key:

```bash
ros2 launch xparo xparo_launch.py \
  xparo_project_id:=<id> xparo_secret_key:=<secret> xparo_environment:=local \
  record_bags:=true
```

This starts a real `rosbag2_recorder` process alongside the node
(`launch/xparo_launch.py`), and `xparo_ros.py` constructs a
`RosbagControl` (`rosbag_control.py`) that drives it through its native
service interface (`Record`/`Stop`/`Resume`/`IsPaused`/
`IsDiscoveryRunning`) with verify-after-action and a watchdog, so it
actually knows whether recording is happening rather than assuming a
subprocess is healthy.

Once running, control it live over its own ROS topic (independent of
Django entirely):

```bash
ros2 topic pub -1 /ros2_bag_control std_msgs/msg/String "data: 'start'"
ros2 topic pub -1 /ros2_bag_control std_msgs/msg/String "data: 'stop'"
ros2 topic echo /ros2_bag_control/recording_status   # current state string
ros2 topic echo /ros2_bag_control/recorder_alive      # Bool
```

Completed sessions upload to cloud storage via `BlackboxOrchestrator`
once `REST_API_TOKEN` has been armed (see the API table above) --
confirm a bag actually lands in the dashboard's project media after a
short `start`/`stop` cycle.

---

## Tethered transport (`tethered_tcp`)

For a physically-tethered vehicle with no path to Django at all. Same
`remote_ops.py` handlers, different wire framing (CRC32-framed TCP with
3-channel redundancy -- see `transports/tethered_tcp.py`'s module
docstring). Test it the same way as section B above, just point a raw TCP
client at the configured channel instead of a WebSocket:

```bash
ros2 launch xparo xparo_launch.py \
  xparo_transport:=tethered_tcp \
  tethered_channels_config_path:=/path/to/tethered_channels.yaml
```

`config/tethered_channels.yaml` in this package is a real example (three
`{name, host, port}` channel entries) to copy and point at your actual
RS-485<->Ethernet bridges. `ros_packages/tools/topside_gcs/` is the
protocol-compatible topside control surface for this mode.

---

## Troubleshooting

- **Node output looks "stuck" but the connection banner never appears,
  or appears very late**: `ros2 launch`/`ros2 run` pipe stdout, which
  Python fully block-buffers by default -- real, already-happened output
  can sit invisible in the buffer. `xparo_ros.py` reconfigures stdout to
  line-buffered at the top of the process for exactly this reason; if
  you're seeing this anyway, confirm you're on a build that includes that
  fix.
- **`colcon build --symlink-install`**: config-sync writes (behavior
  trees, properties, env files pushed from Django) land in
  `install/xparo/lib/python3.12/site-packages/...`, which under
  `--symlink-install` resolves straight back to the git-tracked source
  files under `src/xparo/`. Expect `git status` in this repo to show
  changes to `custom_behaviors/`, `properties/`, `config/` after any live
  test session that syncs config -- `git checkout --` them afterward if
  you don't want to keep the test data.
- **A bare `pytest` crashes with `PluginValidationError`**: see
  [`test/README.md`](test/README.md)'s "plugin
  conflict" section -- this is about the automated suite, not live
  testing, but the same venv hits it either way if you `pip install
  pytest` into a ROS2-sourced environment.
- **A robot never appears "online" despite a live connection**: confirm
  `ROBOT_HEARTBEAT` is actually being sent (every 30s, unconditional --
  see `engine.py`'s `_logging_update_loop`) and that `Robots.is_online`'s
  90s threshold hasn't just not ticked over yet after a very recent
  connect.
