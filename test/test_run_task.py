"""Behaviour Tree redesign Phase 11: the robot-side half of RUN_TASK -> TASK_RESULT.
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

    def test_live_updates_carry_the_runs_run_id(self):
        """Finding F8 (MEDIUM): confirmed live -- ADD_live_update_bt used to
        omit run_id entirely, so two tasks running concurrently produced
        indistinguishable live node-status updates on the wire; the
        dashboard's live canvas had no way to tell them apart. Every
        ADD_live_update_bt sent during a real RUN_TASK dispatch must carry
        the same run_id as that run's own TASK_RESULT.
        """
        engine = _make_engine(xparo_stage="development")
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(message)
        engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)

        engine.on_ws_message('ws', {"RUN_TASK": {
            "task_id": "abc-live",
            "tree_xml": "<LoadNextDelivery />",
            "blackboard": {},
            "save_task_history": False,
        }})

        task_result = None
        live_updates = []
        for _ in range(50):
            live_updates = []
            for raw in sent:
                parsed = json.loads(raw)
                if "TASK_RESULT" in parsed:
                    task_result = parsed["TASK_RESULT"]
                if "ADD_live_update_bt" in parsed:
                    live_updates.append(parsed["ADD_live_update_bt"])
            if task_result:
                break
            time.sleep(0.05)

        assert task_result is not None, f"TASK_RESULT never arrived; sent={sent}"
        assert live_updates, "no live updates were sent during the run"
        for update in live_updates:
            assert update["run_id"] == task_result["run_id"]

    def test_no_bt_executor_attached_reports_a_clear_non_retryable_failure(self):
        """RUN_TASK must not raise just because this Engine isn't owned by
        a real Xparo node (every test in this repo, and any standalone
        use). It used to be dropped silently, which left the dashboard's
        "Run now" waiting until its own timeout with no explanation; now
        the robot says why, and marks it not retryable so
        restart_on_failure doesn't re-dispatch it in a loop."""
        engine = _make_engine()
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(message)
        assert engine.bt_executor is None

        engine.on_ws_message('ws', {"RUN_TASK": {"task_id": "x", "tree_xml": "<LoadNextDelivery />"}})

        results = [json.loads(m)["TASK_RESULT"] for m in sent if "TASK_RESULT" in json.loads(m)]
        assert len(results) == 1
        assert results[0]["task_id"] == "x"
        assert results[0]["success"] is False
        assert results[0]["outcome"] == "no_executor"
        assert results[0]["retryable"] is False
        assert "ROS 2" in results[0]["error"]


class TestUserMessage:
    """Persona field test (2026-10-14): staff saw "fail_table_unknown
    (AlwaysFailure): AlwaysFailure always returns FAILURE" for an order to a
    table that isn't on the outlet's map. A tree writes what a person should
    read to the user_message blackboard key; it rides along on TASK_RESULT."""

    def test_a_failed_run_carries_the_message_set_in_the_tree(self):
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock())
        responses = []
        tree = ('<Sequence><SetBlackboard output_key="user_message" '
                'value="Table 7 is not on this outlet\'s map. Please serve it by hand."/>'
                '<AlwaysFailure name="fail_table_unknown"/></Sequence>')
        handle_run_task(executor, {"task_id": "t-msg", "tree_xml": tree, "blackboard": {}, "save_task_history": False},
                        responses.append, xparo_stage="development")
        result = responses[0]["TASK_RESULT"]
        assert result["success"] is False
        assert result["user_message"] == "Table 7 is not on this outlet's map. Please serve it by hand."
        assert "fail_table_unknown" in result["explanation"]  # the technical side is still there

    def test_no_message_is_an_empty_string(self):
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock())
        responses = []
        handle_run_task(executor, {"task_id": "t-ok", "tree_xml": "<AlwaysSuccess/>", "blackboard": {},
                                   "save_task_history": False}, responses.append, xparo_stage="development")
        assert responses[0]["TASK_RESULT"]["user_message"] == ""

    def test_a_custom_node_can_set_the_message_as_the_docs_say(self):
        import py_trees as pt
        from xparo.bt_engine.node_registry import NODE_REGISTRY

        class _Says(pt.behaviour.Behaviour):
            def __init__(self, name, blackboard):
                super().__init__(name=name)
                self.blackboard = blackboard

            def update(self):
                self.blackboard["user_message"] = "Navigation is down, serve table 5 by hand."
                return common.Status.FAILURE

        NODE_REGISTRY["_SaysAndFails"] = lambda name, attrs, blackboard, children, ros_node: _Says(name, blackboard)
        try:
            responses = []
            handle_run_task(BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock()),
                            {"task_id": "t-node", "tree_xml": "<_SaysAndFails/>", "blackboard": {}, "save_task_history": False},
                            responses.append, xparo_stage="development")
        finally:
            del NODE_REGISTRY["_SaysAndFails"]
        assert responses[0]["TASK_RESULT"]["user_message"] == "Navigation is down, serve table 5 by hand."


class TestStageVersions:
    """Dashboard stage versions (apps/analytics/bt_versions.py, History ->
    Stages): the RUN_TASK / synced task says which saved version of the tree
    runs; a task whose tree is gone, or whose marked copy is missing on this
    robot, carries tree_error."""

    def _run(self, val, xparo_stage="development", subtree_resolver=None):
        executor = BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock())
        responses, history, started = [], [], []
        handle_run_task(executor, val, responses.append, add_task_history=history.append,
                        xparo_stage=xparo_stage, on_started=started.append, subtree_resolver=subtree_resolver)
        return responses, history, started, executor

    def test_report_and_history_carry_the_version_fields(self):
        val = {"task_id": "t1", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "save_task_history": True,
               "stage": "production", "tree_name": "patrol", "tree_stage": "production", "tree_version": "v-1",
               "tree_version_at": "2026-11-02T14:05:00+00:00", "tree_hash": "abc123"}
        responses, history, started, _ = self._run(val, xparo_stage="production")
        expected = {"tree_stage": "production", "tree_version": "v-1", "tree_version_at": "2026-11-02T14:05:00+00:00",
                    "tree_hash": "abc123"}
        result = responses[0]["TASK_RESULT"]
        assert result["success"] is True
        assert {k: result[k] for k in expected} == expected
        assert {k: started[0]["TASK_STARTED"][k] for k in expected} == expected
        assert {k: history[0]["input_data"][k] for k in expected} == expected
        assert history[0]["input_data"]["tree_name"] == "patrol"
        assert "behaviour" not in result

    def test_old_dashboards_send_none_of_them(self):
        responses, _, _, _ = self._run({"task_id": "t1", "tree_xml": "<LoadNextDelivery />", "blackboard": {}})
        result = responses[0]["TASK_RESULT"]
        assert (result["tree_stage"], result["tree_version"], result["tree_version_at"], result["tree_hash"]) == \
            ("", "", "", "")

    def test_a_marked_run_never_loads_a_subtree_from_disk(self):
        """The server sends every SubTree a marked version runs; a ref the
        payload lacks must not be filled with the robot's working copy."""
        loaded = []

        def disk(name):
            loaded.append(name)
            return "<LoadNextDelivery />"
        val = {"task_id": "t1", "tree_xml": '<SubTree ID="dock_seq"/>', "blackboard": {}, "stage": "development",
               "tree_version": "v-1", "tree_stage": "development"}
        responses, _, _, _ = self._run(val, subtree_resolver=disk)
        assert loaded == []
        assert responses[0]["TASK_RESULT"]["success"] is False
        # a working-copy run still may (today's behaviour)
        val = {"task_id": "t2", "tree_xml": '<SubTree ID="dock_seq"/>', "blackboard": {}, "stage": "development"}
        responses, _, _, _ = self._run(val, subtree_resolver=disk)
        assert set(loaded) == {"dock_seq"} and responses[0]["TASK_RESULT"]["success"] is True

    def test_tree_error_reports_invalid_tree_with_the_dashboard_message(self):
        message = ("Patrol has no Production version. Set one on the Behaviour page (Patrol -> Stage -> "
                   "Production) or pick a specific tree for this task.")
        val = {"task_id": "t1", "tree_xml": "", "blackboard": {}, "stage": "production", "save_task_history": True,
               "tree_name": "Patrol: no production version", "tree_error": message}
        responses, history, _, executor = self._run(val, xparo_stage="production")
        result = responses[0]["TASK_RESULT"]
        assert result["success"] is False
        assert result["outcome"] == "invalid_tree"
        assert result["retryable"] is False      # the server never restarts it
        assert result["error"] == message

    def test_tree_error_wins_even_when_a_file_was_found(self):
        """An old Scratch Space fallback must never run in its place."""
        val = {"task_id": "t1", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "stage": "development",
               "tree_error": "Patrol has no Development version."}
        responses, _, _, _ = self._run(val)
        assert responses[0]["TASK_RESULT"]["outcome"] == "invalid_tree"
        assert responses[0]["TASK_RESULT"]["success"] is False

    def test_tree_error_comes_after_the_stage_check(self):
        val = {"task_id": "t1", "tree_xml": "", "blackboard": {}, "stage": "development",
               "tree_error": "Patrol has no Development version."}
        responses, _, started, _ = self._run(val, xparo_stage="production")
        result = responses[0]["TASK_RESULT"]
        assert result["outcome"] == "stage_mismatch" and result["stage_mismatch"] is True
        assert started == []

