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
    points at -- exposes just enough for GET_ROS2_TOPICS/rosbag dispatch
    to find what they're looking for."""
    def __init__(self, rosbag_control=None, topics=None):
        self.rosbag_control = rosbag_control
        self._topics = topics or []

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


def test_watch_error_logs_enables_the_flag_and_resets_read_position():
    engine = _make_engine()
    engine._error_log_read_position = 123  # simulate a stale position from a previous watch
    assert engine._error_log_watch_enabled is False

    engine.on_ws_message('ws', {"WATCH_ERROR_LOGS": {}})

    assert engine._error_log_watch_enabled is True
    assert engine._error_log_read_position is None


def test_unwatch_error_logs_disables_the_flag():
    engine = _make_engine()
    engine._error_log_watch_enabled = True

    engine.on_ws_message('ws', {"UNWATCH_ERROR_LOGS": {}})

    assert engine._error_log_watch_enabled is False


def test_tail_new_error_log_lines_only_returns_error_and_fatal_lines(tmp_path):
    engine = _make_engine()
    log_dir = tmp_path / "latest_run"
    log_dir.mkdir()
    rosout = log_dir / "rosout.log"
    rosout.write_text(
        "[INFO] [123] [some_node]: everything fine\n"
        "[ERROR] [124] [some_node]: something broke\n"
        "[WARN] [125] [some_node]: a warning, not an error\n"
        "[FATAL] [126] [some_node]: total meltdown\n"
    )
    engine.ROS2_LOG_BASE_DIR = str(tmp_path)
    # Simulates a watch already in progress (position 0 = start of file) --
    # the "brand new watch starts from EOF, not history" behavior is its
    # own separate test below.
    engine._error_log_read_position = 0

    lines = engine._tail_new_error_log_lines()

    assert len(lines) == 2
    assert "something broke" in lines[0]
    assert "total meltdown" in lines[1]


def test_tail_new_error_log_lines_starts_from_end_of_file_not_history(tmp_path):
    """_error_log_read_position starts None -- WATCH_ERROR_LOGS deliberately
    never backfills whatever errors already happened before it was turned
    on."""
    engine = _make_engine()
    log_dir = tmp_path / "latest_run"
    log_dir.mkdir()
    rosout = log_dir / "rosout.log"
    rosout.write_text("[ERROR] [1] [n]: old error, before watch started\n")
    engine.ROS2_LOG_BASE_DIR = str(tmp_path)
    assert engine._error_log_read_position is None

    first_call = engine._tail_new_error_log_lines()
    assert first_call == []  # nothing new -- position jumped straight to EOF

    with open(rosout, 'a') as f:
        f.write("[ERROR] [2] [n]: a new error, after watch started\n")
    second_call = engine._tail_new_error_log_lines()
    assert len(second_call) == 1
    assert "a new error" in second_call[0]


def test_tail_new_error_log_lines_with_no_ros_log_dir_returns_empty(tmp_path):
    engine = _make_engine()
    engine.ROS2_LOG_BASE_DIR = str(tmp_path / "does_not_exist")
    assert engine._tail_new_error_log_lines() == []


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
