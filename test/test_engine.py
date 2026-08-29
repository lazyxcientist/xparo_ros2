"""Covers Phase 2's engine.py fixes: the eval() RCE is gone, record_bags/
BAG_DIR reach XP_Database correctly through the constructor (not as
post-construction attributes that arrived too late to matter), and
REST_API_TOKEN is actually routed to the handler that already existed for
it in database.py but was never reachable.
"""
from unittest.mock import MagicMock, patch

import pytest


def _make_engine(**kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    return Engine("secret", "proj-engine-test", **kwargs)


def test_eval_key_is_not_specially_handled():
    """The old `elif k=="eval": return eval(val)` branch is gone -- an
    "eval" key now falls through to call_message like any other unknown
    key, and critically is never passed to Python's eval().
    """
    engine = _make_engine()
    received = []
    engine.call_message = lambda message, **kwargs: received.append(message)

    # If eval() were still wired up, this would execute os.system and blow
    # up the test run -- the point of the test is that it doesn't.
    engine.on_ws_message('ws', {"eval": "__import__('os').system('true')"})

    assert received == [{"eval": "__import__('os').system('true')"}]


def test_record_bags_true_reaches_orchestrator_construction():
    with patch('xparo.database.BlackboxOrchestrator') as mock_orchestrator_cls, \
         patch('xparo.database.signal.signal'), \
         patch('xparo.database.Thread'):
        engine = _make_engine(record_bags=True, BAG_DIR='/tmp/custom-bag-dir')
        assert engine.local_database.orchestrator is mock_orchestrator_cls.return_value
        # BAG_DIR must be the constructor-supplied one, not the Engine
        # default -- this is the exact bug the constructor-ordering fix
        # closes (BAG_DIR used to only take effect if set *before*
        # XP_Database/BlackboxOrchestrator were constructed).
        mock_orchestrator_cls.assert_called_once()
        assert mock_orchestrator_cls.call_args.args[2] == '/tmp/custom-bag-dir'


def test_record_bags_false_does_not_construct_orchestrator():
    with patch('xparo.database.BlackboxOrchestrator') as mock_orchestrator_cls:
        engine = _make_engine(record_bags=False)
        assert engine.local_database.orchestrator is None
        mock_orchestrator_cls.assert_not_called()


def test_rest_api_token_reaches_dashboard_receive_handler():
    """database.py's dashboard_receive already had a correct REST_API_TOKEN
    handler; on_ws_message's dispatch loop just never routed the key to it.
    """
    engine = _make_engine()
    engine.local_database.orchestrator = MagicMock()

    engine.on_ws_message('ws', {"REST_API_TOKEN": "  tok-123  "})

    assert engine.local_database.orchestrator.API_TOKEN == "tok-123"
    engine.local_database.orchestrator._process_uploads.assert_called_once()


def test_rest_api_token_is_a_noop_without_an_orchestrator():
    engine = _make_engine()
    assert engine.local_database.orchestrator is None
    # Must not raise even though there's nothing to arm.
    engine.on_ws_message('ws', {"REST_API_TOKEN": "tok-123"})


# ------------------------------------------------------------------
# Phase 4: remote_ops.py wiring into on_ws_message's dispatch table.
# remote_ops.py's own tests (test_remote_ops.py) cover the handler bodies
# in isolation; these cover that engine.py actually calls them with the
# right arguments and adapts send_response (dict) <-> private_send (JSON
# string) correctly.
# ------------------------------------------------------------------
def test_default_transport_is_django_ws():
    from xparo.transports.django_ws import DjangoWsTransport
    engine = _make_engine()
    assert isinstance(engine.transport, DjangoWsTransport)


def test_xparo_transport_tethered_tcp_selects_that_transport():
    from xparo.transports.tethered_tcp import TetheredTcpTransport
    # No Django to talk to over this transport (that's the whole reason it
    # exists) -- must not crash XP_Database's construction, which is what
    # this is really testing: getattr(self.transport, 'website_base_url',
    # None) has to tolerate a transport that doesn't define that attribute
    # at all (unlike DjangoWsTransport).
    engine = _make_engine(xparo_transport="tethered_tcp")
    assert isinstance(engine.transport, TetheredTcpTransport)
    assert not hasattr(engine.transport, 'website_base_url')


def test_run_command_dispatches_to_remote_ops(tmp_path):
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"RUN_COMMAND": {"command": "echo hi", "request_id": "r1", "timeout": 5}})
    # Runs in its own thread (matches the original -- must never block the
    # dispatch loop) -- give it a moment to finish and reply.
    import time
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)

    assert len(sent) == 1
    import json
    result = json.loads(sent[0])["COMMAND_RESULT"]
    assert result["request_id"] == "r1"
    assert result["success"] is True
    assert "hi" in result["output"]


def test_run_command_empty_command_replies_immediately_no_thread():
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"RUN_COMMAND": {"command": "   ", "request_id": "r2"}})

    import json
    assert len(sent) == 1
    assert json.loads(sent[0])["COMMAND_RESULT"]["output"] == "(empty command)"


def test_teleop_dispatches_to_remote_ops_and_publishes_joy():
    engine = _make_engine()
    joy_calls = []
    engine.joy_publish = lambda axes, buttons: joy_calls.append((axes, buttons))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"TELEOP": {"axes": [1.0], "buttons": []}})

    assert len(joy_calls) == 1
    axes, buttons = joy_calls[0]
    assert len(axes) == 4 and len(buttons) == 3  # padded, see remote_ops.MIN_JOY_*
    import json
    assert json.loads(sent[0]) == {"TELEOP_ACK": {"success": True}}


def test_list_files_dispatches_against_engine_transfer_dir(tmp_path):
    engine = _make_engine()
    engine.transfer_dir = str(tmp_path)
    engine.file_transfer = __import__('xparo.remote_ops', fromlist=['FileTransferSession']).FileTransferSession(str(tmp_path))
    (tmp_path / "readme.txt").write_text("hi")
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"LIST_FILES": {}})

    import json
    tree = json.loads(sent[0])["FILE_LIST"]["tree"]
    assert tree[0]["name"] == "readme.txt"


def test_delete_file_dispatches_against_engine_transfer_dir(tmp_path):
    engine = _make_engine()
    engine.transfer_dir = str(tmp_path)
    (tmp_path / "doomed.txt").write_text("bye")
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"DELETE_FILE": {"path": "doomed.txt"}})

    import json
    ack = json.loads(sent[0])["DELETE_ACK"]
    assert ack["success"] is True
    assert not (tmp_path / "doomed.txt").exists()


def test_file_transfer_upload_round_trip_through_engine(tmp_path):
    import base64
    import json
    engine = _make_engine()
    engine.transfer_dir = str(tmp_path)
    from xparo import remote_ops
    engine.file_transfer = remote_ops.FileTransferSession(str(tmp_path))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"FILE_REQ": {"filename": "up.bin", "direction": "upload", "size": 5}})
    engine.on_ws_message('ws', {"FILE_CHUNK": {"data": base64.b64encode(b"hello").decode()}})
    engine.on_ws_message('ws', {"FILE_COMPLETE": {}})

    assert (tmp_path / "up.bin").read_bytes() == b"hello"
    last = json.loads(sent[-1])
    assert last["FILE_COMPLETE"]["received"] == 5


# ------------------------------------------------------------------
# Fleet-management popups ported from the AUV GCS -- dispatch wiring only
# (remote_ops.py's own tests cover the handler bodies in isolation).
# ------------------------------------------------------------------
class _FakeBtNode:
    """Stands in for the real rclpy Node (Xparo) engine.bt_executor.node
    points at -- exposes just enough for GET_ROS2_TOPICS/rosbag/
    diagnostics dispatch to find what they're looking for."""
    def __init__(self, rosbag_control=None, topics=None, diagnostics_aggregator=None, rosout_watcher=None):
        self.rosbag_control = rosbag_control
        self._topics = topics or []
        self.diagnostics_aggregator = diagnostics_aggregator
        self.rosout_watcher = rosout_watcher

    def get_topic_names_and_types(self):
        return self._topics


class _FakeBtExecutor:
    def __init__(self, node):
        self.node = node


def test_run_command_passes_max_lines_through(tmp_path):
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"RUN_COMMAND": {
        "command": "python3 -c \"[print(i) for i in range(50)]\"", "request_id": "r3", "max_lines": 5,
    }})

    import time
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    import json
    result = json.loads(sent[0])["COMMAND_RESULT"]
    assert result["max_lines"] == 5
    assert len(result["output"].splitlines()) == 5


def test_reboot_robot_dispatches_to_remote_ops(tmp_path):
    from unittest.mock import patch
    import subprocess as _subprocess
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    with patch('xparo.remote_ops.subprocess.run') as mock_run:
        mock_run.return_value = _subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="a password is required",
        )
        engine.on_ws_message('ws', {"REBOOT_ROBOT": {}})
        import time
        for _ in range(50):
            if sent:
                break
            time.sleep(0.05)

    import json
    result = json.loads(sent[0])["REBOOT_RESULT"]
    assert result["success"] is False
    assert result["needs_password"] is True


def test_get_ros2_topics_dispatches_against_bt_executor_node():
    engine = _make_engine()
    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(topics=[("/foo", ["std_msgs/msg/String"])]))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"GET_ROS2_TOPICS": {}})

    import json
    topics = json.loads(sent[0])["ROS2_TOPICS"]["topics"]
    assert topics == [{"name": "/foo", "types": ["std_msgs/msg/String"]}]


def test_get_ros2_topics_with_no_bt_executor_returns_empty_list():
    engine = _make_engine()
    assert engine.bt_executor is None
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"GET_ROS2_TOPICS": {}})

    import json
    assert json.loads(sent[0]) == {"ROS2_TOPICS": {"topics": []}}


def test_get_rosbag_status_reads_the_live_rosbag_control_off_bt_executor_node():
    from xparo import remote_ops
    engine = _make_engine()
    control = type("FakeControl", (), {"state": "writing", "recorder_alive": True})()
    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(rosbag_control=control))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"GET_ROSBAG_STATUS": {}})

    import json
    status = json.loads(sent[0])["ROSBAG_STATUS"]
    assert status["state"] == "writing"
    assert status["recorder_alive"] is True


def test_get_rosbag_status_with_no_bt_executor_reports_unavailable():
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"GET_ROSBAG_STATUS": {}})

    import json
    status = json.loads(sent[0])["ROSBAG_STATUS"]
    assert status["state"] == "unavailable"
    assert status["recorder_alive"] is False


def test_start_stop_save_rosbag_dispatch_to_the_live_control():
    class FakeControl:
        state = "closed"
        recorder_alive = True
        def __init__(self):
            self.calls = []
        def handle_start(self):
            self.calls.append("start")
            self.state = "writing"
        def handle_stop(self, on_done=None):
            self.calls.append("stop")
            self.state = "closed"

    control = FakeControl()
    engine = _make_engine()
    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(rosbag_control=control))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"START_ROSBAG": {}})
    engine.on_ws_message('ws', {"SAVE_ROSBAG": {}})  # alias for stop
    engine.on_ws_message('ws', {"STOP_ROSBAG": {}})

    assert control.calls == ["start", "stop", "stop"]
    import json
    actions = [json.loads(m)["ROSBAG_ACTION_RESULT"]["action"] for m in sent]
    assert actions == ["start", "save", "stop"]


def _make_rosout_watcher(on_new_error=None):
    """Real RosoutWatcher against a trivial subscription-recording stub
    node (mirrors test_diagnostics_aggregator.py's own FakeNode) -- real
    execution over a hand-rolled fake watcher."""
    from xparo.rosout_watcher import RosoutWatcher

    class _StubNode:
        def create_subscription(self, *a, **k):
            return MagicMock()

    return RosoutWatcher(_StubNode(), on_new_error=on_new_error)


def test_watch_error_logs_enables_the_watcher_and_wires_live_push():
    engine = _make_engine()
    watcher = _make_rosout_watcher()
    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(rosout_watcher=watcher))
    assert watcher.watch_enabled is False

    engine.on_ws_message('ws', {"WATCH_ERROR_LOGS": {}})

    assert watcher.watch_enabled is True
    assert watcher._live_push is not None


def test_watch_error_logs_replays_whatever_was_already_seen_since_boot():
    """Opening the popup shows recent history immediately, not just
    future events -- matches the old (file-tailing) feature's own
    expectation, now sourced from RosoutWatcher's own buffer."""
    from rcl_interfaces.msg import Log
    engine = _make_engine()
    watcher = _make_rosout_watcher()
    watcher._on_log(Log(name='some_node', msg='already happened', level=Log.ERROR))
    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(rosout_watcher=watcher))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"WATCH_ERROR_LOGS": {}})

    import json
    messages = [json.loads(m)["ERROR_LOG_ENTRY"]["message"] for m in sent]
    assert "already happened" in messages


def test_unwatch_error_logs_disables_the_watcher():
    engine = _make_engine()
    watcher = _make_rosout_watcher()
    watcher.watch_enabled = True
    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(rosout_watcher=watcher))

    engine.on_ws_message('ws', {"UNWATCH_ERROR_LOGS": {}})

    assert watcher.watch_enabled is False


def test_watch_error_logs_with_no_bt_executor_is_a_safe_noop():
    engine = _make_engine()
    engine.on_ws_message('ws', {"WATCH_ERROR_LOGS": {}})  # must not raise
    engine.on_ws_message('ws', {"UNWATCH_ERROR_LOGS": {}})  # must not raise


def test_get_live_status_dispatches_to_remote_ops(tmp_path):
    engine = _make_engine()
    engine.local_database.get_smart_resource_consumption = lambda: {
        "cpu_avg_percent": 10.0, "ram_used_percent": 20.0, "disk_used_percent": 30.0, "gpu_percent": None,
    }
    engine.local_database.get_cpu_temperature = lambda: 55.5
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"GET_LIVE_STATUS": {}})
    import time
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)

    import json
    status = json.loads(sent[0])["LIVE_STATUS"]
    assert status["cpu_percent"] == 10.0
    assert status["temp_c"] == 55.5
    assert status["uptime_seconds"] >= 0
    assert "battery" not in status


def test_persisted_credential_is_scoped_to_project_id(tmp_path):
    """Found via a real local-testing session: credential.json used to
    store just {"value": ...}, with no notion of which project it was
    issued for -- so a credential persisted while testing against project
    A kept getting silently used (and silently overriding a freshly
    supplied xparo_secret_key) when later pointed at unrelated project B,
    producing a confusing 403 with no indication the new secret was never
    actually tried. A credential must only be reused for the exact
    project_id it was issued under.
    """
    engine = _make_engine()
    engine.xparo_credential_path = str(tmp_path / "credential.json")
    engine.project_id = "project-a"

    engine._persist_credential("secret-for-project-a")
    assert engine._load_persisted_credential() == "secret-for-project-a"

    engine.project_id = "project-b"
    assert engine._load_persisted_credential() is None

    engine.project_id = "project-a"
    assert engine._load_persisted_credential() == "secret-for-project-a"


def test_persist_credential_writes_the_project_id_alongside_the_value(tmp_path):
    import json as json_module
    engine = _make_engine()
    engine.xparo_credential_path = str(tmp_path / "credential.json")
    engine.project_id = "proj-engine-test"

    engine._persist_credential("some-secret")

    with open(engine.xparo_credential_path) as f:
        stored = json_module.load(f)
    assert stored == {"value": "some-secret", "project_id": "proj-engine-test"}


def test_persisted_credential_wires_the_fallback_callback_into_the_transport():
    """Only a connection actually using a *persisted* credential should get
    a way to fall back -- see _fall_back_to_raw_secret's own docstring for
    why a raw xparo_secret_key launch argument has nothing to fall back to
    (a genuinely wrong/revoked secret should just keep failing normally).
    """
    with patch('xparo.engine.Engine._load_persisted_credential', return_value='persisted-secret'):
        engine = _make_engine()
    assert engine.transport.on_persisted_credential_rejected == engine._fall_back_to_raw_secret


def test_raw_secret_key_does_not_wire_the_fallback_callback():
    with patch('xparo.engine.Engine._load_persisted_credential', return_value=None):
        engine = _make_engine()
    assert engine.transport.on_persisted_credential_rejected is None


def test_fall_back_to_raw_secret_clears_the_file_and_replaces_the_transport(tmp_path):
    """Regression test for a bug confirmed live: a persisted ROBOT_CREDENTIAL
    left over from before its Robots/RobotCredential row was deleted (or
    rotated) server-side gets rejected with a 403 forever -- since
    run_forever(reconnect=N) just keeps retrying the same doomed URL, a
    perfectly valid xparo_secret_key argument would never actually get
    tried without this recovering on its own. Covers the actual recovery:
    the stale file is removed, the dead transport is closed (not left
    retrying alongside the new one), and a fresh transport is built from
    the *original* secret_key argument and told to connect.
    """
    import os

    engine = _make_engine()
    engine.xparo_credential_path = str(tmp_path / "credential.json")
    engine._persist_credential("stale-value")
    assert os.path.exists(engine.xparo_credential_path)

    old_transport = engine.transport
    old_transport.close = MagicMock()

    with patch('xparo.engine.DjangoWsTransport') as mock_transport_cls:
        new_transport = mock_transport_cls.return_value
        engine._fall_back_to_raw_secret()

    old_transport.close.assert_called_once()
    assert not os.path.exists(engine.xparo_credential_path)
    mock_transport_cls.assert_called_once()
    # "secret" is _make_engine()'s raw secret_key -- the whole point is
    # this must be the constructor argument, never the stale persisted one.
    assert mock_transport_cls.call_args.args[0] == "secret"
    assert mock_transport_cls.call_args.args[1] == engine.project_id
    new_transport.connect.assert_called_once()
    assert engine.transport is new_transport


def _make_diagnostics_aggregator():
    """A real DiagnosticsAggregator against a trivial subscription-
    recording stub node (mirrors test_diagnostics_aggregator.py's own
    FakeNode) -- real execution over a hand-rolled fake aggregator."""
    from xparo.diagnostics_aggregator import DiagnosticsAggregator

    class _StubNode:
        def create_subscription(self, *a, **k):
            return MagicMock()

    return DiagnosticsAggregator(_StubNode())


def test_get_diagnostics_snapshot_dispatches_against_bt_executor_node():
    engine = _make_engine()
    aggregator = _make_diagnostics_aggregator()
    # A name GET_DIAGNOSTICS_SNAPSHOT's own _refresh_self_diagnostics call
    # (real psutil disk usage, no rosbag_control here) never touches --
    # simulates an entry that arrived over the real /diagnostics topic.
    aggregator.record_self_status('nav2/amcl', 'warn', 'localization degraded')
    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(diagnostics_aggregator=aggregator))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"GET_DIAGNOSTICS_SNAPSHOT": {}})

    import json
    snapshot = json.loads(sent[0])["DIAGNOSTICS_SNAPSHOT"]
    assert snapshot["components"]["nav2/amcl"]["level"] == "warn"
    assert snapshot["overall_level"] == "warn"


def test_get_diagnostics_snapshot_with_no_bt_executor_reports_empty():
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"GET_DIAGNOSTICS_SNAPSHOT": {}})

    import json
    assert json.loads(sent[0])["DIAGNOSTICS_SNAPSHOT"] == {"components": {}, "overall_level": None}


def test_get_xparo_version_reports_commit_and_distro():
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    import os
    with patch.object(engine.local_database, 'get_xparo_git_commit', return_value='abc123'), \
         patch.dict(os.environ, {'ROS_DISTRO': 'jazzy'}):
        engine.on_ws_message('ws', {"GET_XPARO_VERSION": {}})

    import json
    result = json.loads(sent[0])["XPARO_VERSION"]
    assert result == {"xparo_git_commit": "abc123", "ros_distro": "jazzy"}


def test_refresh_self_diagnostics_records_rosbag_and_disk_status():
    engine = _make_engine()
    aggregator = _make_diagnostics_aggregator()

    class _FakeRosbagControl:
        state = "writing"
        recorder_alive = True

    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(
        rosbag_control=_FakeRosbagControl(), diagnostics_aggregator=aggregator,
    ))

    engine._refresh_self_diagnostics()

    snapshot = aggregator.snapshot()
    assert snapshot["components"]["xparo.rosbag_recorder"]["level"] == "ok"
    assert "state=writing" in snapshot["components"]["xparo.rosbag_recorder"]["message"]
    assert "xparo.disk_usage" in snapshot["components"]


def test_refresh_self_diagnostics_flags_a_dead_recorder_as_an_error():
    engine = _make_engine()
    aggregator = _make_diagnostics_aggregator()

    class _FakeDeadRosbagControl:
        state = "unknown"
        recorder_alive = False

    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(
        rosbag_control=_FakeDeadRosbagControl(), diagnostics_aggregator=aggregator,
    ))

    engine._refresh_self_diagnostics()

    assert aggregator.snapshot()["components"]["xparo.rosbag_recorder"]["level"] == "error"


def test_refresh_self_diagnostics_with_no_bt_executor_is_a_safe_noop():
    engine = _make_engine()
    engine._refresh_self_diagnostics()  # must not raise


def test_build_heartbeat_payload_includes_diagnostics_level():
    engine = _make_engine()
    aggregator = _make_diagnostics_aggregator()

    class _FakeRosbagControl:
        state = "closed"
        recorder_alive = True

    engine.bt_executor = _FakeBtExecutor(_FakeBtNode(
        rosbag_control=_FakeRosbagControl(), diagnostics_aggregator=aggregator,
    ))

    payload = engine._build_heartbeat_payload()

    assert payload["ROBOT_HEARTBEAT"]["device_id"] == engine.local_database.unique_id
    assert payload["ROBOT_HEARTBEAT"]["diagnostics_level"] in ("ok", "warn", "error", "stale")


def test_build_heartbeat_payload_diagnostics_level_is_none_with_no_bt_executor():
    engine = _make_engine()
    payload = engine._build_heartbeat_payload()
    assert payload["ROBOT_HEARTBEAT"]["diagnostics_level"] is None


def test_fall_back_to_raw_secret_tolerates_an_already_missing_file(tmp_path):
    """Defensive: if the file was already removed (e.g. by another process,
    or a previous call), this must not raise -- only ever meant to run
    once per Engine instance in practice (on_ws_error only fires the
    callback once), but should be safe regardless.
    """
    engine = _make_engine()
    engine.xparo_credential_path = str(tmp_path / "does-not-exist.json")
    engine.transport.close = MagicMock()

    with patch('xparo.engine.DjangoWsTransport'):
        engine._fall_back_to_raw_secret()  # must not raise
