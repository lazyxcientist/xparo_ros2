"""Regression tests for the robot-side items of the 2026-10 breakage
review (hurried beginners breaking things in production). Each test names
the review item it guards."""
import threading
from unittest.mock import MagicMock

import pytest

from xparo.bt_engine import run_task


@pytest.fixture
def running_t1():
    event = threading.Event()
    with run_task._active_lock:
        run_task._active_runs["existing"] = {"task_id": "t1", "event": event, "started": 0, "reason": ""}
    yield
    with run_task._active_lock:
        run_task._active_runs.pop("existing", None)


# ---- #4: the same task must not run twice at once on one robot ----------
def test_second_run_of_a_running_task_is_refused(running_t1):
    executor, results = MagicMock(), []
    run_task.handle_run_task(executor, {"task_id": "t1", "tree_xml": "<X/>", "stage": "production"},
                             results.append)
    result = results[0]["TASK_RESULT"]
    assert result["outcome"] == "already_running"
    assert result["retryable"] is False          # restart_on_failure must not loop on it
    assert not any(c for c in executor.mock_calls if "run" in str(c))


def test_allow_parallel_opts_out(running_t1):
    results = []
    run_task.handle_run_task(MagicMock(), {"task_id": "t1", "tree_xml": "", "stage": "production",
                                           "allow_parallel": True}, results.append)
    assert results[0]["TASK_RESULT"]["outcome"] != "already_running"


def test_a_different_task_still_runs(running_t1):
    results = []
    run_task.handle_run_task(MagicMock(), {"task_id": "t2", "tree_xml": "", "stage": "production"},
                             results.append)
    assert results[0]["TASK_RESULT"]["outcome"] != "already_running"


# ---- #12: a custom Python node with bad top-level code must not brick boot ----
def _plugin(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(
        "from xparo.bt_engine.plugin_loader import CustomBTNode\n" + body +
        f"\nclass Good_{name}(CustomBTNode):\n    XML_TAG = 'Good_{name}'\n")
    return str(path)


def test_sys_exit_at_import_is_skipped_not_fatal(tmp_path):
    from xparo.bt_engine import plugin_loader
    bad = _plugin(tmp_path, "exits", "import sys\nsys.exit(3)\n")
    good = _plugin(tmp_path, "fine", "")
    registry = plugin_loader.load_plugins([bad, good])
    assert "Good_fine" in registry and "Good_exits" not in registry


def test_endless_loop_at_import_times_out(tmp_path, monkeypatch):
    from xparo.bt_engine import plugin_loader
    monkeypatch.setattr(plugin_loader, "PLUGIN_IMPORT_TIMEOUT_S", 0.5)
    stuck = _plugin(tmp_path, "loops", "import time\nwhile True:\n    time.sleep(0.05)\n")
    good = _plugin(tmp_path, "fine2", "")
    registry = plugin_loader.load_plugins([stuck, good])
    assert list(registry) == ["Good_fine2"]


# ---- #14 / #66: network calls have time limits; REST mode never dies ----
def test_rest_heartbeat_post_has_a_timeout():
    from unittest.mock import patch
    from xparo.transports import django_ws
    t = django_ws.DjangoWsTransport.__new__(django_ws.DjangoWsTransport)
    t.website_full_url, t.connection_type, t.on_message = "http://x/", "rest", lambda *a: None
    with patch.object(django_ws.requests, "post") as post:
        post.return_value.status_code = 500
        t.send("{}", command_for="rest")
    assert post.call_args.kwargs["timeout"] == django_ws.HTTP_TIMEOUT


def test_rest_polling_survives_errors_and_backs_off():
    from unittest.mock import patch, MagicMock
    from xparo.transports import django_ws
    t = django_ws.DjangoWsTransport.__new__(django_ws.DjangoWsTransport)
    t.website_full_url, t.connection_type = "http://x/", "rest"
    t.on_message, t.on_connected = MagicMock(), MagicMock()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 4:
            t._rest_running = False
    ok = MagicMock(status_code=201)
    ok.json.return_value = {"status": 1}
    with patch.object(django_ws.requests, "get", side_effect=[OSError("no wifi"), OSError("no wifi"), ok, ok]), \
         patch.object(django_ws.requests, "post", side_effect=OSError("no wifi")):
        t.start_reset_framework(sleep=sleep)
    assert sleeps[:2] == [10, 20]                      # backing off while unreachable
    assert sleeps[2] == django_ws.REST_POLL_INTERVAL_SECONDS  # back to normal once it answers
    t.on_connected.assert_called_once()


# ---- #11 / #97: topic callbacks never kill the node; offline asks answer ----
def _bare_node():
    from xparo import xparo_ros
    node = xparo_ros.Xparo.__new__(xparo_ros.Xparo)
    node._logger = MagicMock()
    node.get_logger = lambda: node._logger
    node.xparo_engine = MagicMock()
    node.dash_send = MagicMock()
    return node


class Msg:
    def __init__(self, data):
        self.data = data


@pytest.mark.parametrize("data", ['42', '"t1"', '[1,2]', '{"task_id": ["x"]}',
                                  '{"task_id": "t1", "override_params": "fast"}', 'not json'])
def test_bad_run_task_messages_are_logged_not_fatal(data):
    node = _bare_node()
    node._guarded(node.run_task_topic_cb)(Msg(data))     # must not raise
    calls = node.xparo_engine.run_task_from_topic.call_args_list
    if data == '"t1"':
        assert calls[0].args == ("t1", None)              # a bare id is accepted
    else:
        assert calls == []


def test_guarded_logs_unexpected_errors_instead_of_raising():
    node = _bare_node()

    def boom(msg):
        raise KeyError("x")
    node._guarded(boom)(Msg("hi"))
    assert node._logger.error.called


def test_asking_while_offline_answers_at_once_and_keeps_case():
    node = _bare_node()
    node.xparo_engine.is_online.return_value = False
    node.call_message = MagicMock()
    node.ask_question_fun(Msg("Where is Room 3?"))
    reply = node.call_message.call_args.args[0]["message"]
    assert reply["error"] == "offline"
    node.xparo_engine.send.assert_not_called()
    node.xparo_engine.is_online.return_value = True
    node.ask_question_fun(Msg("Where is Room 3?"))
    node.xparo_engine.send.assert_called_once_with("Where is Room 3?")   # not upper-cased


def test_cancel_task_topic_stops_a_running_task(running_t1):
    node = _bare_node()
    node.cancel_task_cb(Msg("t1"))
    with run_task._active_lock:
        assert run_task._active_runs["existing"]["event"].is_set()


# ---- #31 / #98 / P29: message logging, response noise, per-key isolation ----
def _engine():
    from test_engine import _make_engine
    return _make_engine()


def test_received_messages_are_logged_by_key_and_size_only(capsys):
    engine = _engine()
    # keys with no side effects: the point is only what gets printed
    engine.on_ws_message('ws', {"status": {"secret": "s3cr3t-value"}, "System": "tok-123"})
    out = capsys.readouterr().out
    assert "status" in out and "System" in out
    assert "s3cr3t-value" not in out and "tok-123" not in out


def test_only_assistant_replies_reach_xparo_response():
    engine = _engine()
    published = []
    engine.call_message = lambda message, **kw: published.append(message)
    engine.on_ws_message('ws', {"status": "x", "System": {"big": "sync"}})
    assert published == []
    engine.on_ws_message('ws', {"message": {"bot_response": "hi"}})
    assert published == [{"message": {"bot_response": "hi"}}]


def test_one_failing_key_does_not_skip_the_rest():
    engine = _engine()
    seen = []
    original = engine._on_ws_key

    def flaky(k, val, message):
        if k == "first":
            raise OSError("disk full")
        seen.append(k)
        return original(k, val, message)
    engine._on_ws_key = flaky
    engine.on_ws_message('ws', {"first": 1, "message": {"bot_response": "still here"}})
    assert seen == ["message"]


def test_non_json_text_is_ignored():
    _engine().on_ws_message('ws', "not json {")   # must not raise


# ---- #33 / #34 / #35: results survive restarts; heartbeat and files survive faults ----
def test_unsent_results_survive_a_restart():
    engine = _engine()
    engine._try_send_dict = lambda payload: False          # offline
    engine._send_important_dict({"TASK_RESULT": {"task_id": "t1", "success": True}})
    reborn = _engine()                                     # power cut, new process
    assert reborn._pending_important_sends == [{"TASK_RESULT": {"task_id": "t1", "success": True}}]
    sent = []
    reborn._try_send_dict = lambda payload: sent.append(payload) or True
    reborn._flush_pending_important_sends()
    assert sent and _engine()._pending_important_sends == []   # cleared once delivered


def test_outbox_is_bounded(monkeypatch):
    from xparo import engine as engine_module
    monkeypatch.setattr(engine_module, "MAX_OUTBOX_MESSAGES", 3)
    engine = _engine()
    engine._try_send_dict = lambda payload: False
    for i in range(5):
        engine._send_important_dict({"n": i})
    assert [p["n"] for p in engine._pending_important_sends] == [2, 3, 4]


def test_heartbeat_loop_survives_errors(monkeypatch):
    from xparo import engine as engine_module
    engine = _engine()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) >= 3:
            raise SystemExit  # stop the endless loop for the test
        raise OSError("No space left on device")
    engine.private_send = boom
    monkeypatch.setattr(engine_module.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit):
        engine._logging_update_loop()
    assert len(calls) == 3                                  # kept going after failures


def test_atomic_write_keeps_old_content_when_writing_fails(tmp_path, monkeypatch):
    from xparo import fileutil
    target = tmp_path / "tasks.json"
    fileutil.atomic_write_json(str(target), {"t1": {}})
    monkeypatch.setattr(fileutil.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        fileutil.atomic_write_json(str(target), {"t2": {}})
    assert target.read_text() == '{"t1": {}}'
    assert [p.name for p in tmp_path.iterdir()] == ["tasks.json"]   # no temp files left behind


# ---- #36: a node that blocks can't hold off the time limit or Cancel ----
@pytest.fixture
def blocking_node():
    import py_trees
    from py_trees import common
    from xparo.bt_engine.node_registry import NODE_REGISTRY
    release = threading.Event()

    class _Blocks(py_trees.behaviour.Behaviour):
        def update(self):
            release.wait(30)           # a custom node doing time.sleep(600)
            return common.Status.SUCCESS

    NODE_REGISTRY["_BlocksForever"] = lambda name, attrs, blackboard, children, ros_node: _Blocks(name=name or "b")
    yield
    release.set()
    del NODE_REGISTRY["_BlocksForever"]


def test_time_limit_stops_a_blocked_node(blocking_node):
    import time
    from xparo.bt_engine.executor import BehaviorTreeExecutor
    executor = BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock())
    started = time.monotonic()
    report = executor.run_with_trace("<_BlocksForever />", timeout_s=0.5, tick_rate_hz=0)
    assert report["outcome"] == "timeout"
    assert time.monotonic() - started < 3


def test_cancel_stops_a_blocked_node(blocking_node):
    import time
    from xparo.bt_engine.executor import BehaviorTreeExecutor
    executor = BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock())
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    report = executor.run_with_trace("<_BlocksForever />", cancel_event=cancel, tick_rate_hz=0)
    assert report["outcome"] == "cancelled"
    assert time.monotonic() - started < 3


# ---- #3 / #99: cloned SD cards get their own identity; a new dongle doesn't ----
def test_identity_is_kept_across_boots_and_a_new_wifi_dongle(tmp_path):
    from xparo import robot_identity
    ids = iter(["id-1", "id-2"])
    first, moved = robot_identity.resolve(str(tmp_path), lambda: next(ids), macs={"aa", "bb"})
    assert (first, moved) == ("id-1", False)
    # dongle "bb" swapped for "cc": same machine (aa still there), same id
    assert robot_identity.resolve(str(tmp_path), lambda: next(ids), macs={"aa", "cc"}) == ("id-1", False)
    # and now cc counts as this machine too
    assert robot_identity.resolve(str(tmp_path), lambda: next(ids), macs={"cc"}) == ("id-1", False)


def test_cloned_card_on_other_hardware_gets_a_new_identity(tmp_path):
    from xparo import robot_identity
    robot_identity.resolve(str(tmp_path), lambda: "original", macs={"aa"})
    device_id, moved = robot_identity.resolve(str(tmp_path), lambda: "clone", macs={"zz"})
    assert (device_id, moved) == ("clone", True)


def test_a_clone_drops_the_copied_credential(tmp_path, monkeypatch):
    import json
    import os
    from xparo import robot_identity
    from test_engine import _make_engine
    engine = _make_engine()
    db_dir = engine._state_dir
    with open(os.path.join(db_dir, robot_identity.IDENTITY_FILE), "w") as f:
        json.dump({"device_id": "original", "macs": ["11:11:11:11:11:11"]}, f)
    with open(engine.xparo_credential_path, "w") as f:
        json.dump({"value": "original-robot-credential", "project_id": engine.project_id}, f)
    try:
        clone = _make_engine()
        assert clone.device_id != "original"
        assert not os.path.exists(engine.xparo_credential_path)
    finally:
        for p in (engine.xparo_credential_path, os.path.join(db_dir, robot_identity.IDENTITY_FILE)):
            if os.path.exists(p):
                os.remove(p)


# ---- #32 / #63 / #64: recordings on a full disk, failed uploads, wrong clocks ----
def _orchestrator(tmp_path, state="writing", uri=None):
    from xparo.blackbox_manager import BlackboxOrchestrator
    from xparo.rosbag_control import WRITING, CLOSED
    control = MagicMock()
    control.state = WRITING if state == "writing" else CLOSED
    control.current_session_uri = uri
    o = BlackboxOrchestrator(ROBOT_ID="r", xparo_website_url="http://x", BAG_DIR=str(tmp_path), rosbag_control=control)
    o.API_TOKEN = "t"
    return o


def _bag(tmp_path, name, mtime):
    import os
    d = tmp_path / name
    d.mkdir()
    f = d / f"{name}_0.mcap"
    f.write_bytes(b"x" * 10)
    os.utime(f, (mtime, mtime))
    return str(f)


def test_the_open_session_is_found_by_path_even_if_the_clock_jumped(tmp_path):
    # the session being recorded has an OLDER mtime than a finished bag (clock rolled back)
    open_bag = _bag(tmp_path, "bag_new_ab12", 1000)
    done_bag = _bag(tmp_path, "bag_old", 5000)
    o = _orchestrator(tmp_path, uri=str(tmp_path / "bag_new_ab12"))
    assert o._open_session_bags([open_bag, done_bag]) == {open_bag}


def test_cleanup_never_deletes_the_open_session(tmp_path, monkeypatch):
    import os
    from xparo import blackbox_manager
    open_bag = _bag(tmp_path, "bag_open", 1000)
    done_bag = _bag(tmp_path, "bag_done", 500)
    o = _orchestrator(tmp_path, uri=str(tmp_path / "bag_open"))
    usage = iter([95.0, 95.0, 60.0])
    monkeypatch.setattr(o, "get_disk_usage", lambda: next(usage, 60.0))
    Usage = type("U", (), {"total": 100, "used": 95})
    monkeypatch.setattr(blackbox_manager.shutil, "disk_usage", lambda p: Usage)
    monkeypatch.setattr(os.path, "getsize", lambda p: 50)      # deleting finished bags helps
    o._force_cleanup()
    assert os.path.exists(open_bag) and not os.path.exists(done_bag)


def test_disk_full_of_other_files_stops_recording_instead_of_wiping_bags(tmp_path, monkeypatch):
    import os
    from xparo import blackbox_manager
    done_bag = _bag(tmp_path, "bag_done", 500)
    o = _orchestrator(tmp_path, state="closed")
    o.stop_recording = MagicMock()
    Usage = type("U", (), {"total": 1000, "used": 990})
    monkeypatch.setattr(blackbox_manager.shutil, "disk_usage", lambda p: Usage)
    o._force_cleanup()                                        # bags are tiny: deleting them won't help
    assert os.path.exists(done_bag)
    o.stop_recording.assert_called_once()


def test_failed_upload_backs_off_instead_of_resending_every_cycle(tmp_path, monkeypatch):
    from xparo import blackbox_manager
    _bag(tmp_path, "bag_done", 500)
    o = _orchestrator(tmp_path, state="closed")
    monkeypatch.setattr(blackbox_manager, "build_sensor_data", lambda p: {})
    posts = []
    monkeypatch.setattr(blackbox_manager.requests, "post",
                        lambda *a, **k: posts.append(1) or MagicMock(status_code=402, text="no credits"))
    monkeypatch.setattr(o, "get_disk_usage", lambda: 10.0)
    o._run_cycle()
    o._run_cycle()                                            # still inside the back-off window
    assert len(posts) == 1 and o._upload_failures == 1


def test_manager_loop_survives_a_failing_cycle(tmp_path, monkeypatch):
    from xparo import blackbox_manager
    o = _orchestrator(tmp_path, state="closed")
    calls = []

    def cycle():
        calls.append(1)
        if len(calls) == 2:
            o.running = False
        raise OSError("bag folder deleted")
    o._run_cycle = cycle
    monkeypatch.setattr(blackbox_manager.time, "sleep", lambda s: None)
    o.manage_disk_and_upload()
    assert len(calls) == 2


# ---- #39 / #67 / #68 / #89 / #90: setup mistakes and stuck reconnect loops ----
def test_pasted_keys_are_cleaned_and_checked():
    from xparo.xparo_ros import clean_setting, check_connection_settings
    assert clean_setting('  "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"\n') == "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
    assert check_connection_settings("1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", "abc123") == []
    assert check_connection_settings("", "") and len(check_connection_settings("", "")) == 2
    assert "doesn't look like" in check_connection_settings("my project", "abc")[0]
    assert "spaces" in check_connection_settings("1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", "ab c")[0]


def test_a_second_copy_of_the_node_is_refused(tmp_path):
    from xparo.xparo_ros import acquire_single_instance_lock
    lock_path = str(tmp_path / "xparo.lock")
    first = acquire_single_instance_lock(lock_path)
    assert first is not None
    import subprocess
    import sys
    # a different process (like a second `ros2 run xparo ...`) can't take it
    code = ("import fcntl,sys; f=open(sys.argv[1],'a+');\n"
            "try:\n fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB); print('got')\n"
            "except OSError: print('refused')")
    out = subprocess.run([sys.executable, "-c", code, lock_path], capture_output=True, text=True).stdout.strip()
    assert out == "refused"
    first.close()
    assert acquire_single_instance_lock(lock_path) is not None   # free again once the first exits


def test_running_with_sudo_is_warned_about(monkeypatch, tmp_path):
    from xparo import xparo_ros
    monkeypatch.setattr(xparo_ros.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "priya")
    assert any("sudo" in p for p in xparo_ros.check_run_environment(str(tmp_path)))


def test_reconnect_delay_is_spread_between_robots():
    from xparo.transports import django_ws
    assert 5 <= django_ws.RECONNECT_DELAY_SECONDS <= 12


def test_a_credential_rejected_later_falls_back_to_the_secret_key(monkeypatch):
    from xparo.transports import django_ws
    t = django_ws.DjangoWsTransport.__new__(django_ws.DjangoWsTransport)
    t._websocket_connected = False
    t.ws = None
    fallback = MagicMock()
    t.on_persisted_credential_rejected = fallback
    clock = [0.0]
    monkeypatch.setattr(django_ws.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(t, "_credential_rejected", lambda: True)

    def sleep(s):
        clock[0] += s
    t._credential_watchdog_loop(sleep=sleep, check_every=15)
    fallback.assert_called_once()
    assert clock[0] >= django_ws.CREDENTIAL_CHECK_AFTER_SECONDS


# ---- #61: live updates never block the tree ----
def test_live_updates_do_not_block_when_the_link_is_slow():
    import time
    engine = _engine()
    gate = threading.Event()
    engine.private_send = lambda data, **kw: gate.wait(5)     # a link that hangs
    started = time.monotonic()
    for i in range(engine.LIVE_UPDATE_QUEUE_SIZE + 50):
        engine.add_live_update({"node_name": f"n{i}", "curr": "RUNNING"})
    assert time.monotonic() - started < 1                      # the tick loop wasn't held up
    assert engine._live_dropped >= 49                          # overflow dropped, not queued forever
    gate.set()


# ---- #62: a big download doesn't hold up other commands ----
def test_file_download_runs_in_the_background():
    import time
    engine = _engine()
    gate = threading.Event()
    engine.file_transfer = MagicMock()
    engine.file_transfer.handle_file_req.side_effect = lambda req, send: gate.wait(5)
    started = time.monotonic()
    engine.on_ws_message('ws', {"FILE_REQ": {"filename": "big_folder", "direction": "download"}})
    assert time.monotonic() - started < 1          # the message loop is free for Cancel/teleop
    gate.set()


# ---- #60: C++ nodes aren't recompiled when nothing changed ----
def test_cpp_node_is_compiled_once_per_change(tmp_path, monkeypatch):
    import os
    import subprocess
    from xparo.bt_engine import runners
    compiles = []

    def fake_gpp(cmd, **kw):
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("binary")
        os.chmod(out, 0o755)
        compiles.append(out)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(runners.subprocess, "run", fake_gpp)
    src = "class Greet : public BT::SyncActionNode {};"
    path, err = runners.compile_cpp_node(src, "", str(tmp_path), "greet")
    assert err is None and os.path.exists(path)
    runners.compile_cpp_node(src, "", str(tmp_path), "greet")          # reconnect: same source
    assert len(compiles) == 1
    runners.compile_cpp_node(src + " // v2", "", str(tmp_path), "greet")  # edited: rebuild
    assert len(compiles) == 2
    assert not os.path.exists(path + ".building")                   # swapped in, not left behind


# ---- #58: switching a robot to another project ----
def test_switching_projects_sets_the_old_projects_files_aside(tmp_path):
    from xparo.project_files import retire_other_project_files
    beh, envs = tmp_path / "custom_behaviors", tmp_path / "custom_envs"
    (beh / "custom_aiml").mkdir(parents=True)
    (beh / "custom_aiml" / "patrol.xml").write_text("<old/>")
    (beh / "tasks.json").write_text("{}")
    (envs / "custom_maps").mkdir(parents=True)
    assert retire_other_project_files(str(beh), str(envs), "project-a") is None   # first run: just remember
    moved = retire_other_project_files(str(beh), str(envs), "project-b")
    assert moved and not (beh / "custom_aiml").exists() and not (beh / "tasks.json").exists()
    assert (tmp_path / moved / "behaviors" / "custom_aiml" / "patrol.xml").read_text() == "<old/>"  # kept, not deleted
    assert retire_other_project_files(str(beh), str(envs), "project-b") is None   # same project again: nothing moves


# ---- #13: a power cut during an OTA update ----
def _launch_module():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launch", "xparo_launch.py")
    spec = importlib.util.spec_from_file_location("xparo_launch_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_interrupted_update_is_rolled_back_at_the_next_launch(tmp_path):
    from xparo import ota
    journal = str(tmp_path / "ota_journal.json")
    ota.write_journal({"repo": "/ws/src/xparo", "workspace": "/ws", "from": "aaaa1111", "to": "bbbb2222",
                       "symlink_install": True}, path=journal)
    ran = []
    ok = _launch_module().recover_interrupted_update(journal, run=lambda argv, cwd: ran.append((argv, cwd)) or 0,
                                                      log=lambda m: None)
    assert ok is True
    # --keep: other uncommitted edits survive (round 2, V21)
    assert ran[0] == (["git", "reset", "--keep", "aaaa1111"], "/ws/src/xparo")
    assert ran[1] == (["colcon", "build", "--packages-select", "xparo", "--symlink-install"], "/ws")
    import os
    assert not os.path.exists(journal)


def test_no_journal_means_a_normal_start(tmp_path):
    ran = []
    assert _launch_module().recover_interrupted_update(str(tmp_path / "none.json"), run=lambda *a: ran.append(a)) is None
    assert ran == []


# ---- #28: rosbag uploads use the robot's own credential ----
def test_uploads_authenticate_as_the_robot(tmp_path, monkeypatch):
    from xparo import blackbox_manager
    o = _orchestrator(tmp_path, state="closed")
    o.use_robot_credential("robot-cred-123")
    _bag(tmp_path, "bag_done", 500)
    monkeypatch.setattr(blackbox_manager, "build_sensor_data", lambda p: {})
    sent = {}
    monkeypatch.setattr(blackbox_manager.requests, "post",
                        lambda url, **kw: sent.update(kw) or MagicMock(status_code=201))
    monkeypatch.setattr(o, "get_disk_usage", lambda: 10.0)
    o._run_cycle()
    assert sent["headers"]["Authorization"] == "RobotToken robot-cred-123"


# ---- #29: secrets never leave the robot through the params popup ----
def test_secret_parameters_are_hidden_in_the_params_list(monkeypatch):
    import subprocess
    from xparo import remote_ops
    node = MagicMock()
    node.get_node_names_and_namespaces.return_value = [("xparo", "/")]
    dump = "/xparo:\n  ros__parameters:\n    xparo_secret_key: abc123\n    xparo_project_id: p1\n    wifi_password: hunter2\n"
    monkeypatch.setattr(remote_ops.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, dump, ""))
    out = []
    remote_ops.handle_list_ros2_params(node, out.append)
    values = {p["name"]: p["value"] for p in out[0]["ROS2_PARAMS"]["params"]}
    assert values["xparo_secret_key"] == "(hidden)" and values["wifi_password"] == "(hidden)"
    assert values["xparo_project_id"] == "p1"
