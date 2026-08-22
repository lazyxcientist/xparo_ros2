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
from xparo.bt_engine.run_task import ALLOWED_TASK_STAGES, handle_run_task


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
            # A development robot runs any task stage -- this test is
            # about success reporting, not stage gating (see
            # TestHandleRunTaskStageGating below for that).
            xparo_stage="development",
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
                xparo_stage="development",
            )
        finally:
            del NODE_REGISTRY["_AlwaysFailsForRunTask"]

        # Genuinely ticked and failed, not rejected for a stage mismatch
        # (which would also report success=False, but for the wrong
        # reason -- see TestHandleRunTaskStageGating for that case).
        assert "stage_mismatch" not in responses[0]["TASK_RESULT"]
        assert responses[0]["TASK_RESULT"]["success"] is False
        # A clean FAILURE status is not an exception -- final_blackboard
        # never gets an "_error" key for this case, so TASK_RESULT's error
        # must be an empty string, not missing (the frontend consumer
        # distinguishes "no error message" from "no error key at all").
        assert responses[0]["TASK_RESULT"]["error"] == ""

    def test_a_malformed_tree_reports_success_false_instead_of_raising(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        responses = []

        handle_run_task(
            executor, {"task_id": "t3", "tree_xml": "<NotARealTag />"}, responses.append,
            xparo_stage="development",
        )

        assert "stage_mismatch" not in responses[0]["TASK_RESULT"]
        assert responses[0]["TASK_RESULT"]["success"] is False
        # Unlike a clean FAILURE, this genuinely raised (UnknownNodeError) --
        # the real exception text must reach TASK_RESULT, not just
        # success=False with no explanation.
        assert "NotARealTag" in responses[0]["TASK_RESULT"]["error"]

    def test_save_task_history_true_calls_add_task_history_with_the_resolved_blackboard(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        history_calls = []

        handle_run_task(
            executor,
            {"task_id": "t4", "tree_xml": '<Script code="x := 1 + 2" />', "blackboard": {}, "save_task_history": True},
            lambda x: None,
            add_task_history=history_calls.append,
            xparo_stage="development",
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
            xparo_stage="development",
        )

        assert history_calls == []


class TestAllowedTaskStagesCascade:
    """Matches the user-specified cascade exactly: development runs
    everything; each stricter stage only runs itself and stricter still."""

    def test_development_robot_runs_every_stage(self):
        assert ALLOWED_TASK_STAGES["development"] == {
            "development", "testing", "review", "production",
        }

    def test_testing_robot_cannot_run_development_tasks(self):
        assert ALLOWED_TASK_STAGES["testing"] == {"testing", "review", "production"}

    def test_review_robot_only_runs_review_and_production(self):
        assert ALLOWED_TASK_STAGES["review"] == {"review", "production"}

    def test_production_robot_only_runs_production_tasks(self):
        assert ALLOWED_TASK_STAGES["production"] == {"production"}


class TestHandleRunTaskStageGating:
    def test_a_production_robot_rejects_a_development_task_without_running_it(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        executor.run = MagicMock(side_effect=AssertionError("must not tick a rejected task"))
        responses = []

        handle_run_task(
            executor,
            {"task_id": "t6", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "stage": "development"},
            responses.append,
            xparo_stage="production",
        )

        result = responses[0]["TASK_RESULT"]
        assert result["success"] is False
        assert result["stage_mismatch"] is True
        assert "production" in result["error"] and "development" in result["error"]
        executor.run.assert_not_called()

    def test_a_development_robot_runs_a_production_task(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        responses = []

        handle_run_task(
            executor,
            {"task_id": "t7", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "stage": "production"},
            responses.append,
            xparo_stage="development",
        )

        result = responses[0]["TASK_RESULT"]
        assert result["success"] is True
        assert "stage_mismatch" not in result

    def test_a_task_with_no_stage_at_all_is_treated_as_production_not_silently_allowed(self):
        """A stale locally-cached task from before this feature shipped
        (task_sync.py's own default) has no "stage" key -- must not fail
        open."""
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        executor.run = MagicMock(side_effect=AssertionError("must not tick a rejected task"))
        responses = []

        handle_run_task(
            executor,
            {"task_id": "t8", "tree_xml": "<LoadNextDelivery />", "blackboard": {}},
            responses.append,
            xparo_stage="testing",
        )

        result = responses[0]["TASK_RESULT"]
        assert result["success"] is False
        assert result["stage_mismatch"] is True

    def test_rejected_task_never_calls_add_task_history(self):
        mock_engine = MagicMock()
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=mock_engine)
        history_calls = []

        handle_run_task(
            executor,
            {"task_id": "t9", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "stage": "development", "save_task_history": True},
            lambda x: None,
            add_task_history=history_calls.append,
            xparo_stage="production",
        )

        assert history_calls == []


class TestRunTaskOnWsMessageRoundTrip:
    def test_full_round_trip_through_on_ws_message(self):
        # development runs any task stage -- this test is about the
        # dispatch plumbing, not stage gating.
        engine = _make_engine(xparo_stage="development")
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
