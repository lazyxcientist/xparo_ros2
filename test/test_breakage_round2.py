"""Regression tests for round 2 of the breakage review (robot side).
Each test names the finding it guards ([V] = robot-hardware persona)."""
import json
from unittest.mock import MagicMock, patch

from xparo.transports import django_ws


def _transport(connection_type="rest"):
    # The real constructor: round-1 tests built the transport with __new__,
    # which is how V4/V5 (bugs in __init__) slipped through.
    return django_ws.DjangoWsTransport("secret-abc", "proj-1", on_message=MagicMock(),
                                       on_connected=MagicMock(), connection_type=connection_type)


def test_rest_mode_actually_polls():
    """V4: connection_type rest stopped before its first request."""
    t = _transport("rest")
    calls = []

    def fake_get(*a, **kw):
        calls.append(kw)
        t._rest_running = False
        return MagicMock(status_code=201, content=b'{"a": 1}', json=lambda: {"a": 1})
    with patch.object(django_ws.requests, "get", side_effect=fake_get), \
            patch.object(django_ws.requests, "post"), \
            patch.object(django_ws.threading, "Thread") as thread:
        t.connect()
        target = thread.call_args.kwargs["target"]
        assert t._rest_running is True
        target(sleep=lambda s: None)
    assert calls and t.on_message.call_count == 1


def test_credential_watchdog_runs_until_closed():
    """V5: _closed started True, so the watchdog exited immediately."""
    t = _transport("websocket")
    assert t._closed is False
    t.close()
    assert t._closed is True


def test_an_unchanged_rest_sync_is_not_processed_again():
    """V6: every poll rewrote every tree on the SD card."""
    t = _transport("rest")
    body = MagicMock(content=b'{"x": 1}', json=lambda: {"x": 1})
    assert t._on_rest_body(body) is True
    assert t._on_rest_body(body) is False
    assert t.on_message.call_count == 1


def test_the_secret_is_not_in_any_url():
    """K3/V25: URLs (and so access logs) carried the robot's secret."""
    t = _transport("websocket")
    assert "secret-abc" not in t.socket_full_url and "secret-abc" not in t.website_full_url
    assert t.auth_headers == {django_ws.SECRET_HEADER: "secret-abc"}


def _engine():
    from test_engine import _make_engine
    return _make_engine()


def test_results_sent_late_are_marked_replayed():
    """V2: a stale failure from the outbox restarted the task by itself."""
    engine = _engine()
    sent = []
    engine.private_send = lambda data, **kw: sent.append(json.loads(data))
    engine.transport = MagicMock(websocket_connected=True)
    engine._pending_important_sends = [{"TASK_RESULT": {"task_id": "t", "success": False}}]
    engine._flush_pending_important_sends()
    assert sent == [{"TASK_RESULT": {"task_id": "t", "success": False, "replayed": True}}]


def test_a_rest_send_counts_as_delivered():
    """V24: in REST fallback every result was sent twice."""
    engine = _engine()
    engine.private_send = lambda data, **kw: None
    engine.transport = MagicMock(websocket_connected=False, connection_type="hybrid", rest_fallback_active=True)
    assert engine._try_send_dict({"x": 1}) is True


def test_a_stopped_run_stays_registered_until_its_blocked_node_returns():
    """V3: the run was released while the blocked node still ran, so the
    same task could start again and drive alongside it."""
    import threading
    import py_trees
    from py_trees import common
    from xparo.bt_engine import executor as ex
    from xparo.bt_engine.node_registry import NODE_REGISTRY
    release = threading.Event()

    class _Blocks(py_trees.behaviour.Behaviour):
        def update(self):
            release.wait(10)
            return common.Status.SUCCESS
    NODE_REGISTRY["_BlocksR2"] = lambda name, attrs, blackboard, children, ros_node: _Blocks(name=name or "b")
    old_grace, ex.ABANDON_GRACE_S = ex.ABANDON_GRACE_S, 0.1
    try:
        report = ex.BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock()).run_with_trace(
            "<_BlocksR2 />", timeout_s=0.3, tick_rate_hz=0)
        stopped = report["_still_ticking"]
        assert report["outcome"] == "timeout" and not stopped.is_set()
        release.set()
        assert stopped.wait(5)
    finally:
        ex.ABANDON_GRACE_S = old_grace
        release.set()
        del NODE_REGISTRY["_BlocksR2"]


def test_one_failing_terminate_does_not_skip_the_others():
    """V27: root.stop() ended at the first terminate() that raised."""
    import py_trees
    from py_trees import common
    from xparo.bt_engine.executor import _stop_running
    stopped = []

    class _Node(py_trees.behaviour.Behaviour):
        def __init__(self, name, fail):
            super().__init__(name=name)
            self.fail = fail

        def update(self):
            return common.Status.RUNNING

        def terminate(self, new_status):
            if new_status == common.Status.INVALID and self.status == common.Status.RUNNING:
                stopped.append(self.name)
                if self.fail:
                    raise RuntimeError("motor driver gone")
    a, b = _Node("a", True), _Node("b", False)
    root = py_trees.composites.Parallel("p", policy=py_trees.common.ParallelPolicy.SuccessOnAll(), children=[a, b])
    root.tick_once()
    errors = []
    _stop_running(root, errors)
    assert set(stopped) == {"a", "b"} and errors


def test_a_timed_out_command_stops_its_children_too(tmp_path):
    """V8: only the shell was killed; what it started kept running."""
    import subprocess
    import time
    import pytest
    from xparo.proc import run_group
    marker = tmp_path / "still-running"
    # the child keeps touching a file every 0.1 s for 5 s
    cmd = f"(for i in $(seq 50); do touch {marker}; sleep 0.1; done) & wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_group(cmd, 0.5, shell=True)
    marker.unlink(missing_ok=True)
    time.sleep(0.5)
    assert not marker.exists()


def test_a_tree_deleted_on_the_dashboard_is_deleted_here(tmp_path):
    """V13: deleted trees stayed on the robot and stayed runnable."""
    engine = _engine()
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    folder = tmp_path / "custom_aiml"
    folder.mkdir()
    (folder / "patrol.xml").write_text("<root/>")
    (folder / "keep.xml").write_text("<root/>")
    engine.on_ws_message("ws", {"DELETE_synced_files": {"folder": "custom_aiml", "names": ["patrol", "../../etc/passwd"]}})
    assert not (folder / "patrol.xml").exists() and (folder / "keep.xml").exists()


def test_node_discovery_never_runs_the_file():
    """V14: discovering node files imported (ran) every stray .py file."""
    from xparo.bt_engine.plugin_loader import static_node_tags
    source = (
        "raise SystemExit('this must never run')\n"
        "class Grip(CustomBTNode):\n    XML_TAG = 'Grip'\n"
        "class Helper:\n    XML_TAG = 'NotANode'\n"
    )
    assert static_node_tags(source) == {"Grip": "Grip"}
    assert static_node_tags("def broken(:") == {}


def test_a_second_copy_waits_for_the_lock_instead_of_exiting(tmp_path):
    """V28: the second copy exited and launch respawned it every 3 s."""
    from xparo.xparo_ros import acquire_single_instance_lock, wait_for_instance_lock
    path = str(tmp_path / "xparo.lock")
    first = acquire_single_instance_lock(path)
    waits = []

    def sleep(s):
        waits.append(s)
        first.close()          # the first copy stops
    handle = wait_for_instance_lock(path, retry_s=30, sleep=sleep)
    assert handle is not None and waits == [30]


def test_update_recovery_gives_up_after_a_few_failures(tmp_path):
    """V21: every launch repeated a failing 30-minute rebuild."""
    import os
    from xparo import ota
    from test_breakage_robot import _launch_module
    launch = _launch_module()
    journal = str(tmp_path / "ota_journal.json")
    ota.write_journal({"repo": "/ws/src/xparo", "workspace": "/ws", "from": "aaaa1111", "to": "bbbb2222"}, path=journal)
    for _ in range(launch.MAX_RECOVERY_ATTEMPTS):
        assert launch.recover_interrupted_update(journal, run=lambda argv, cwd: 1, log=lambda m: None) is False
        assert os.path.exists(journal)
    assert launch.recover_interrupted_update(journal, run=lambda argv, cwd: 1, log=lambda m: None) is False
    assert not os.path.exists(journal)


def test_a_shared_ros_domain_is_called_out(monkeypatch, tmp_path):
    """V1: on the default domain every robot shares /joy and task topics."""
    from xparo.xparo_ros import check_run_environment, other_xparo_nodes
    monkeypatch.delenv("ROS_DOMAIN_ID", raising=False)
    assert any("ROS_DOMAIN_ID" in p for p in check_run_environment(str(tmp_path)))
    monkeypatch.setenv("ROS_DOMAIN_ID", "12")
    assert not any("ROS_DOMAIN_ID" in p for p in check_run_environment(str(tmp_path)))
    assert other_xparo_nodes([("xparo", "/"), ("xparo", "/"), ("talker", "/")]) == [("xparo", "/")]
    assert other_xparo_nodes([("xparo", "/")]) == []


def test_file_chunks_out_of_order_are_refused_and_downloads_carry_seq(tmp_path):
    """V11: chunks relayed in any order were written garbled and reported ok."""
    import base64
    from xparo.remote_ops import FileTransferSession
    sent = []
    s = FileTransferSession(str(tmp_path))
    s.handle_file_req({"filename": "a.bin", "direction": "upload", "size": 2}, sent.append)
    s.handle_file_chunk({"data": base64.b64encode(b"x").decode(), "seq": 1}, sent.append)
    assert "error" in sent[-1] and not (tmp_path / "a.bin").exists()

    s.handle_file_req({"filename": "b.bin", "direction": "upload", "size": 2}, sent.append)
    for i in range(2):
        s.handle_file_chunk({"data": base64.b64encode(b"y").decode(), "seq": i}, sent.append)
    s.handle_file_complete(sent.append)
    assert sent[-1]["FILE_COMPLETE"]["status"] == "ok" and (tmp_path / "b.bin").read_bytes() == b"yy"

    sent.clear()
    s.handle_file_req({"filename": "b.bin", "direction": "download", "token": "t1"}, sent.append)
    assert sent[0]["FILE_REQ"]["token"] == "t1"
    assert sent[1]["FILE_CHUNK"]["seq"] == 0 and sent[1]["FILE_CHUNK"]["token"] == "t1"
    assert sent[-1]["FILE_COMPLETE"]["token"] == "t1"


def test_ros2_node_logs_are_read(tmp_path, monkeypatch):
    """V32: only ROS 1's rosout.log was read -- ROS 2 never writes it."""
    from xparo.database import XP_Database
    logs = tmp_path / "log"
    run = logs / "2026-10-26-09-00-00-000000-bot-123"
    run.mkdir(parents=True)
    (run / "launch.log").write_text("launch started\n")
    (logs / "bt_executor_123_1.log").write_text("[ERROR] it broke\n")
    monkeypatch.setenv("ROS_LOG_DIR", str(logs))
    db = XP_Database.__new__(XP_Database)
    db.log_pointers_file = str(tmp_path / "pointers.json")
    first = db.get_ros2_jazzy_logs()["new_entries"]
    assert "[ERROR] it broke\n" in first and "launch started\n" in first
    assert db.get_ros2_jazzy_logs()["new_entries"] == []     # nothing new
    with open(logs / "bt_executor_123_1.log", "a") as f:
        f.write("[WARN] again\n")
    assert db.get_ros2_jazzy_logs()["new_entries"] == ["[WARN] again\n"]


def test_ota_build_runs_at_low_priority(monkeypatch):
    """V35: the rebuild competed with navigation at full priority."""
    from xparo import ota
    seen = {}
    monkeypatch.setattr(ota, "_run", lambda argv, timeout, cwd=None: (seen.setdefault("argv", argv), (0, ""))[1])
    monkeypatch.setattr(ota.shutil, "which", lambda name: "/usr/bin/" + name)
    assert ota._build({"workspace": "/ws"})[0]
    assert seen["argv"][:3] == ["nice", "-n", "15"] and "colcon" in seen["argv"]


def test_ads_pause_when_the_robot_has_not_heard_from_the_server_for_days(tmp_path):
    """N15: a robot offline after the venue withdrew consent kept playing for days."""
    import time as _time
    from test_ads import FakePlayer, MON_10AM, entry, make_manager
    from xparo.ads import manager as ads_manager
    player = FakePlayer()
    manager, _ = make_manager(tmp_path, player)
    manager.items = [entry("xparo-a")]
    manager.schedule_at = _time.time() - ads_manager.MAX_SCHEDULE_AGE_S - 60
    manager.tick(MON_10AM)
    assert player.started == []
    manager.schedule_at = _time.time()
    manager.tick(MON_10AM)
    assert len(player.started) == 1


def test_rosbags_stream_to_storage_instead_of_loading_into_memory(tmp_path):
    """V10: requests built a multipart body of the whole bag in RAM."""
    import io
    from xparo.blackbox_manager import BlackboxOrchestrator
    bag = tmp_path / "s" / "s_0.mcap"
    bag.parent.mkdir()
    bag.write_bytes(b"x" * 1000)
    o = BlackboxOrchestrator(ROBOT_ID="r", xparo_website_url="https://srv", BAG_DIR=str(tmp_path))
    o.API_TOKEN = "tok"
    calls = []

    def post(url, **kw):
        calls.append(url)
        if url.endswith("/init/"):
            assert kw["data"]["size"] == 1000
            return MagicMock(status_code=201, json=lambda: {"upload_url": "https://r2/put", "object_key": "k"})
        assert json.loads(kw["data"]["data"]) == {"m": 1}
        return MagicMock(status_code=201)
    with patch("xparo.blackbox_manager.requests.post", side_effect=post), \
            patch("xparo.blackbox_manager.requests.put", return_value=MagicMock(status_code=200)) as put:
        assert o._upload_bag(str(bag), {"m": 1}) == (True, "")
    assert isinstance(put.call_args.kwargs["data"], io.BufferedReader)      # a file, streamed
    assert calls == ["https://srv/api/upload_rosbag/init/", "https://srv/api/upload_rosbag/complete/"]

    # No bucket on the server: big bags stay on the robot rather than going through RAM.
    o.LEGACY_UPLOAD_MAX_BYTES = 10
    with patch("xparo.blackbox_manager.requests.post", return_value=MagicMock(status_code=503)) as legacy:
        ok, message = o._upload_bag(str(bag), {})
    assert not ok and legacy.call_count == 1 and "kept on the robot" in message


def test_the_credential_lives_outside_the_install_tree(tmp_path):
    """V19/V20: credential.json sat in install/share/xparo/config -- a clean
    rebuild deleted it and the robot could no longer sign in."""
    from test_engine import _make_engine
    engine = _make_engine()
    assert not engine.xparo_credential_path.startswith(engine.xparo_folder)
    engine.xparo_credential_path = str(tmp_path / "new" / "credential-proj.json")
    engine._legacy_credential_path = str(tmp_path / "old" / "credential.json")
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "credential.json").write_text(json.dumps({"value": "cred-1", "project_id": engine.project_id}))
    assert engine._load_persisted_credential() == "cred-1"
    assert json.loads((tmp_path / "new" / "credential-proj.json").read_text())["value"] == "cred-1"
    engine._forget_credential_files()
    assert not (tmp_path / "old" / "credential.json").exists() and engine._load_persisted_credential() is None


def test_ads_follow_the_venue_clock_not_the_os_zone(tmp_path):
    """N16: a robot left on UTC played the 9am ad at 2:30pm in India."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from test_ads import FakePlayer
    from xparo.ads.manager import AdManager, utc_offset_minutes
    from test_ads import FakeCapture
    manager = AdManager(str(tmp_path / "ads"), send=lambda p: True, backend=FakePlayer(), capture=FakeCapture())
    manager.store.localize = lambda schedule: ({"ads": [], "timezone": schedule.get("timezone")}, [])
    manager.update_schedule({"ads": [], "timezone": "Asia/Kolkata"}, wait=True)
    venue_now = _dt.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    assert abs((manager.now() - venue_now).total_seconds()) < 5
    assert manager._utc_offset() == 330 == utc_offset_minutes("Asia/Kolkata")
    again = AdManager(str(tmp_path / "ads"), send=lambda p: True, backend=FakePlayer(), capture=FakeCapture())
    assert again.site_timezone == "Asia/Kolkata"      # kept across restarts


def test_the_ad_player_opens_only_when_there_is_an_ad():
    """V36: every robot with a screen got an always-on-top player window,
    with or without ads (developers' desktops too)."""
    from xparo.ads.backends import make_backend
    from xparo.ads.backends import native
    opened = []
    with patch.object(native.NativeBackend, "start", lambda self, m: opened.append(m) or setattr(self, "available", True)):
        backend = make_backend("auto")
        backend.env = {"DISPLAY": ":0"}
        backend.start("manager")
        assert opened == [] and backend.available
        backend.play({"id": "xparo-1"}, "bottom", lambda *a: None)
        assert opened == ["manager"]
        backend.play({"id": "xparo-2"}, "bottom", lambda *a: None)
        assert opened == ["manager"]          # once
    headless = make_backend("auto")
    headless.env = {}
    headless.start("manager")
    assert not headless.available
