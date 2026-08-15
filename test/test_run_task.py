"""Behaviour Tree redesign Phase 11 (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): the robot-side half of RUN_TASK -> TASK_RESULT.
_make_engine/on_ws_message-round-trip style mirrors test_engine.py's own
RUN_COMMAND tests exactly.
"""
import json
import time
from unittest.mock import MagicMock

from py_trees import common

from xparo.bt_engine.executor import BehaviorTreeExecutor
from xparo.bt_engine.run_task import handle_run_task


def _make_engine(**kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    return Engine("secret", "proj-run-task-test", **kwargs)


class TestHandleRunTaskDirect:
    def test_success_sends_task_result_with_success_true(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        responses = []

        handle_run_task(
            executor,
            {"task_id": "t1", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "save_task_history": False},
            responses.append,
        )

        assert len(responses) == 1
        result = responses[0]["TASK_RESULT"]
        assert result["task_id"] == "t1"
        assert result["success"] is True
        assert result["duration_s"] >= 0

    def test_a_tree_that_fails_reports_success_false(self):
        import py_trees as pt

        class _Fail(pt.behaviour.Behaviour):
            def update(self):
                return common.Status.FAILURE

        from xparo.bt_engine.node_registry import NODE_REGISTRY
        NODE_REGISTRY["_AlwaysFailsForRunTask"] = lambda name, attrs, blackboard, children, ros_node: _Fail(name=name)
        try:
            mock_engine = MagicMock()
            executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
            responses = []
            handle_run_task(
                executor,
                {"task_id": "t2", "tree_xml": "<_AlwaysFailsForRunTask />", "blackboard": {}},
                responses.append,
            )
        finally:
            del NODE_REGISTRY["_AlwaysFailsForRunTask"]

        assert responses[0]["TASK_RESULT"]["success"] is False

    def test_a_malformed_tree_reports_success_false_instead_of_raising(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        responses = []

        handle_run_task(executor, {"task_id": "t3", "tree_xml": "<NotARealTag />"}, responses.append)

        assert responses[0]["TASK_RESULT"]["success"] is False

    def test_save_task_history_true_calls_add_task_history_with_the_resolved_blackboard(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        history_calls = []

        handle_run_task(
            executor,
            {"task_id": "t4", "tree_xml": '<Script code="x := 1 + 2" />', "blackboard": {}, "save_task_history": True},
            lambda x: None,
            add_task_history=history_calls.append,
        )

        assert len(history_calls) == 1
        call = history_calls[0]
        assert call["input_data"]["task_id"] == "t4"
        assert call["output_data"]["success"] is True
        assert call["output_data"]["blackboard"]["x"] == 3

    def test_save_task_history_false_never_calls_add_task_history(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        history_calls = []

        handle_run_task(
            executor,
            {"task_id": "t5", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "save_task_history": False},
            lambda x: None,
            add_task_history=history_calls.append,
        )

        assert history_calls == []


class TestRunTaskOnWsMessageRoundTrip:
    def test_full_round_trip_through_on_ws_message(self):
        engine = _make_engine()
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(message)
        engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)

        engine.on_ws_message('ws', {"RUN_TASK": {
            "task_id": "abc-123",
            "tree_xml": "<LoadNextDelivery />",
            "blackboard": {},
            "save_task_history": False,
        }})

        # add_live_update fires (and sends) for every real status change
        # *during* the run, before TASK_RESULT is sent at the very end --
        # wait for that specific key, not just "sent has anything in it".
        task_result = None
        for _ in range(50):
            for raw in sent:
                parsed = json.loads(raw)
                if "TASK_RESULT" in parsed:
                    task_result = parsed["TASK_RESULT"]
                    break
            if task_result:
                break
            time.sleep(0.05)

        assert task_result is not None, f"TASK_RESULT never arrived; sent={sent}"
        assert task_result["task_id"] == "abc-123"
        assert task_result["success"] is True

    def test_no_bt_executor_attached_is_a_safe_noop(self):
        """Matches TELEOP's own posture for a live-node-less Engine (its
        default joy_publish is a no-op) -- RUN_TASK must not raise just
        because this Engine isn't owned by a real Xparo node (every test
        in this repo, and any standalone use -- see this file's own
        __main__ block)."""
        engine = _make_engine()
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(message)
        assert engine.bt_executor is None

        engine.on_ws_message('ws', {"RUN_TASK": {"task_id": "x", "tree_xml": "<LoadNextDelivery />"}})

        time.sleep(0.1)
        assert sent == []
