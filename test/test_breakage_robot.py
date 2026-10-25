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
