"""Node lifecycle (KUNAL-9, 2026-10-31 field test): kunal-mr2's log showed
"Failed to publish: publisher's context is invalid", the closed banner and
then nothing -- the operator read it as the node dying on its own. It was a
`robot.sh stop` (SIGTERM) by a teammate, but the stop looked like a crash:
a dashboard message still arriving on the websocket after the context was
shut down printed that publish error, and a later stop ended in an RCLError
traceback out of rclpy.spin. A real crash must bring the node back without
a site visit (the launch respawns it).

xparo_ros.py looks its share directory up at import time, so it is imported
here with that lookup pointed at a temp dir.
"""
import os
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

rclpy = pytest.importorskip("rclpy")

PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def xparo_ros(tmp_path):
    with patch("ament_index_python.packages.get_package_share_directory", return_value=str(tmp_path)):
        from xparo import xparo_ros
    return xparo_ros


def test_a_message_arriving_during_shutdown_is_dropped_quietly(xparo_ros, capsys):
    publisher = MagicMock()
    publisher.publish.side_effect = RuntimeError("Failed to publish: publisher's context is invalid")
    node = SimpleNamespace(context=SimpleNamespace(ok=lambda: False), dash_send=publisher)

    xparo_ros.Xparo.call_message(node, {"update_success": True})

    publisher.publish.assert_not_called()
    assert capsys.readouterr().out == ""


def test_a_message_is_published_while_running(xparo_ros):
    publisher = MagicMock()
    node = SimpleNamespace(context=SimpleNamespace(ok=lambda: True), dash_send=publisher)

    xparo_ros.Xparo.call_message(node, {"update_success": True})

    assert publisher.publish.call_args[0][0].data == '{"update_success": true}'


def _fake_node():
    engine = MagicMock(ad_manager=None)
    return MagicMock(xparo_engine=engine, restart_requested=False)


def test_a_context_shut_down_under_spin_is_a_clean_stop(xparo_ros):
    node = _fake_node()
    error = RuntimeError("failed to initialize wait set: the given context is not valid")
    with patch.object(xparo_ros, "Xparo", return_value=node), \
            patch.object(xparo_ros.rclpy, "init"), patch.object(xparo_ros.rclpy, "try_shutdown"), \
            patch.object(xparo_ros.rclpy, "spin", side_effect=error), \
            patch.object(xparo_ros.rclpy, "ok", return_value=False):
        xparo_ros.main()

    node.xparo_engine.transport.close.assert_called_once()
    node.xparo_engine.local_database.stop_logging_session.assert_called_once()


def test_a_real_crash_still_raises_so_the_launch_respawns_it(xparo_ros):
    with patch.object(xparo_ros, "Xparo", return_value=_fake_node()), \
            patch.object(xparo_ros.rclpy, "init"), patch.object(xparo_ros.rclpy, "try_shutdown"), \
            patch.object(xparo_ros.rclpy, "spin", side_effect=ValueError("bug")), \
            patch.object(xparo_ros.rclpy, "ok", return_value=True):
        with pytest.raises(ValueError):
            xparo_ros.main()


def test_the_launch_respawns_the_xparo_node():
    launch = open(os.path.join(PACKAGE, "launch", "xparo_launch.py")).read()
    block = re.search(r"Node\(\s*package='xparo',\s*executable='xparo_ros'.*?\)\s*,", launch, flags=re.S)
    assert block, "xparo_ros Node() not found in the launch file"
    node_block = launch[block.start():launch.find("###", block.start())]
    assert re.search(r"respawn\s*=\s*True", node_block)


def _run_task_node(xparo_ros):
    """The bits of an Xparo node run_task_topic_cb and _on_task_event use,
    around a real Engine (rejections go through it to on_task_event)."""
    from xparo.engine import Engine
    engine = Engine("secret", "proj-run-task-cb-test", connection_type="offline")
    node = SimpleNamespace(xparo_engine=engine, logger=MagicMock(), task_result_pub=MagicMock())
    node.get_logger = lambda: node.logger
    engine.on_task_event = lambda kind, info: xparo_ros.Xparo._on_task_event(node, kind, info)
    return node


@pytest.mark.parametrize("data", [
    '{oops',                                          # unparseable
    '{"task_id": 12345}',                             # wrong type
    '{"task_id": "t1", "robot": ["a"]}',              # used to get no answer at all
    '{"task_id": "t1", "allow_parallel": "yes"}',     # used to count as true
    '{"task_id": "t1", "override_params": [1]}',      # type rejection inside the engine
])
def test_every_run_task_rejection_answers_and_is_one_error_on_rosout(xparo_ros, data):
    """ROSAPI-13: parse errors were ERROR on /rosout, size/type rejections
    only printed to stdout; a list "robot" got no task_result at all."""
    import json
    node = _run_task_node(xparo_ros)

    xparo_ros.Xparo.run_task_topic_cb(node, SimpleNamespace(data=data))

    published = [json.loads(c[0][0].data) for c in node.task_result_pub.publish.call_args_list]
    assert [p["outcome"] for p in published] == ["invalid_request"]
    errors = [c[0][0] for c in node.logger.error.call_args_list]
    assert len(errors) == 1 and errors[0].startswith("/xparo/run_task rejected: ")
    node.logger.warn.assert_not_called()


def _node_with_tasks(xparo_ros, tmp_path, tasks):
    from xparo.bt_engine.executor import BehaviorTreeExecutor
    node = _run_task_node(xparo_ros)
    engine = node.xparo_engine
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)
    engine.sync_custom_tasks(tasks)
    engine._send_important_dict = lambda payload: None
    return node


def test_an_unknown_task_is_a_rejection_on_rosout_not_a_failed_task(xparo_ros, tmp_path):
    """ROSAPI-18: unknown_task was a WARN "Task '<id>' FAILED (...)" (and a
    "last task failed" in Health & Errors) where every other client error
    is one ERROR "/xparo/run_task rejected: ..."."""
    import json
    node = _node_with_tasks(xparo_ros, tmp_path, {})

    xparo_ros.Xparo.run_task_topic_cb(node, SimpleNamespace(data='{"task_id": "does-not-exist"}'))

    published = [json.loads(c[0][0].data) for c in node.task_result_pub.publish.call_args_list]
    assert [p["outcome"] for p in published] == ["unknown_task"]
    errors = [c[0][0] for c in node.logger.error.call_args_list]
    assert len(errors) == 1 and errors[0].startswith("/xparo/run_task rejected: Task 'does-not-exist'")
    node.logger.warn.assert_not_called()
    assert node.xparo_engine._last_task_outcome is None


def test_a_busy_answer_names_the_task_and_the_dashboard_cancel(xparo_ros, tmp_path):
    """ROSAPI-18: a double tap was logged as WARN "Task '<task id>' FAILED
    (busy, not started)" and advised a cancel that ROS doesn't have."""
    import json
    import threading
    import time
    from xparo.bt_engine import run_task
    node = _node_with_tasks(xparo_ros, tmp_path, {"t1": {
        "title": "Ravi deliver", "behaviour_tree_name": "", "blackboard_mapping": {}, "params": []}})
    with run_task._active_lock:
        run_task._active_runs["already"] = {"task_id": "t1", "event": threading.Event(), "started": time.monotonic(),
                                            "reason": ""}
    try:
        xparo_ros.Xparo.run_task_topic_cb(node, SimpleNamespace(data='{"task_id": "t1"}'))
    finally:
        with run_task._active_lock:
            run_task._active_runs.pop("already", None)

    published = [json.loads(c[0][0].data) for c in node.task_result_pub.publish.call_args_list]
    assert [p["outcome"] for p in published] == ["busy"]
    assert "Cancel on the task's Task Overview tab in the dashboard" in published[0]["error"]
    assert "cancel it first" not in published[0]["error"]
    infos = [c[0][0] for c in node.logger.info.call_args_list]
    assert infos == ["Task 'Ravi deliver' not started: it is already running on this robot (busy)"]
    node.logger.warn.assert_not_called()
    node.logger.error.assert_not_called()
    assert node.xparo_engine._last_task_outcome is None
