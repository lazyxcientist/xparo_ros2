"""BT.CPP v4 / Groot2 built-in nodes (bt_engine/builtins.py), checks before
a run (tree_builder.validate_tree), the execution report
(BehaviorTreeExecutor.run_with_trace) and task runs that time out, get
cancelled or can't start (run_task.py / engine.py)."""
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from xparo.bt_engine import expr, run_task, tree_builder
from xparo.bt_engine.builtins import TrackedBlackboard
from xparo.bt_engine.executor import BehaviorTreeExecutor


def _executor():
    return BehaviorTreeExecutor(node=None, engine=MagicMock())


def _run(xml, **kwargs):
    kwargs.setdefault("tick_rate_hz", 0)
    return _executor().run_with_trace(xml, **kwargs)


class TestActionsAndConditions:
    def test_set_and_unset_blackboard(self):
        r = _run('<Sequence><SetBlackboard output_key="a" value="1"/><SetBlackboard output_key="{b}" value="{a}"/>'
                 '<UnsetBlackboard key="a"/></Sequence>')
        assert r["outcome"] == "success"
        assert r["blackboard"] == {"b": "1"}

    def test_always_success_and_failure(self):
        assert _run("<AlwaysSuccess/>")["success"] is True
        assert _run("<AlwaysFailure/>")["outcome"] == "failed"

    def test_script_condition_compares_text_numbers_as_numbers(self):
        # Values set from XML (SetBlackboard, task params) arrive as text.
        r = _run('<Sequence><SetBlackboard output_key="x" value="5"/><ScriptCondition code="x &gt; 3"/></Sequence>')
        assert r["outcome"] == "success"

    def test_script_condition_false_names_the_node_and_the_expression(self):
        r = _run('<Sequence name="main"><ScriptCondition name="battery_ok" code="battery &gt; 50"/></Sequence>',
                 blackboard={"battery": 20})
        assert r["outcome"] == "failed"
        assert r["failed_node"]["name"] == "battery_ok"
        assert "battery > 50" in r["failed_node"]["reason"]

    def test_script_condition_with_a_broken_expression_fails_instead_of_crashing(self):
        r = _run('<ScriptCondition code="missing_var &gt; 1"/>')
        assert r["outcome"] == "failed"
        assert "undefined variable" in r["failed_node"]["reason"]

    def test_sleep_waits_then_succeeds(self):
        started = time.monotonic()
        r = _run('<Sleep msec="50"/>', tick_rate_hz=100)
        assert r["outcome"] == "success"
        assert time.monotonic() - started >= 0.05

    def test_was_entry_updated(self):
        r = _run('<Sequence><SetBlackboard output_key="goal" value="a"/><WasEntryUpdated entry="goal"/></Sequence>')
        assert r["outcome"] == "success"
        assert _run('<WasEntryUpdated entry="never_set"/>')["outcome"] == "failed"


class TestDecorators:
    def test_inverter_force_success_force_failure(self):
        assert _run("<Inverter><AlwaysFailure/></Inverter>")["success"] is True
        assert _run("<ForceSuccess><AlwaysFailure/></ForceSuccess>")["success"] is True
        r = _run("<ForceFailure><AlwaysSuccess/></ForceFailure>")
        assert r["outcome"] == "failed"
        assert r["failed_node"]["tag"] == "ForceFailure"

    def test_repeat_runs_the_child_n_times(self):
        r = _run('<Sequence><Script code="n := 0"/><Repeat num_cycles="4"><Script code="n += 1"/></Repeat></Sequence>')
        assert r["blackboard"]["n"] == 4
        script_row = [row for row in r["nodes"] if row["tag"] == "Script"][1]
        assert script_row["ticks"] == 4

    def test_repeat_stops_on_failure(self):
        r = _run('<Repeat num_cycles="5"><AlwaysFailure/></Repeat>')
        assert r["outcome"] == "failed"

    def test_retry_until_successful_retries_and_counts_attempts(self):
        r = _run('<RetryUntilSuccessful num_attempts="10"><Sequence><Script code="k := k + 1"/>'
                 '<ScriptCondition code="k &gt;= 3"/></Sequence></RetryUntilSuccessful>', blackboard={"k": 0})
        assert r["outcome"] == "success"
        assert r["blackboard"]["k"] == 3

    def test_retry_minus_one_means_forever_not_fail_immediately(self):
        """py_trees' Retry (used before) failed on the very first failure
        for num_attempts="-1"; BT.CPP retries forever."""
        r = _run('<RetryUntilSuccessful num_attempts="-1"><Sequence><Script code="k := k + 1"/>'
                 '<ScriptCondition code="k &gt;= 60"/></Sequence></RetryUntilSuccessful>', blackboard={"k": 0})
        assert r["outcome"] == "success"
        assert r["blackboard"]["k"] == 60

    def test_retry_gives_up_after_num_attempts(self):
        r = _run('<RetryUntilSuccessful num_attempts="3"><AlwaysFailure name="nope"/></RetryUntilSuccessful>')
        assert r["outcome"] == "failed"
        assert r["failed_node"]["name"] == "nope"
        assert [row for row in r["nodes"] if row["name"] == "nope"][0]["ticks"] == 3

    def test_keep_running_until_failure(self):
        r = _run('<KeepRunningUntilFailure><Sequence><Script code="k := k + 1"/><ScriptCondition code="k &lt; 3"/>'
                 '</Sequence></KeepRunningUntilFailure>', blackboard={"k": 0})
        assert r["outcome"] == "failed"
        assert r["blackboard"]["k"] == 3

    def test_timeout_fails_a_slow_child_and_halts_it(self):
        r = _run('<Timeout msec="50"><Sleep name="slow" msec="2000"/></Timeout>', tick_rate_hz=100)
        assert r["outcome"] == "failed"
        assert r["failed_node"]["tag"] == "Timeout"
        assert "timed out" in r["failed_node"]["reason"]
        assert [row for row in r["nodes"] if row["name"] == "slow"][0]["status"] == "HALTED"

    def test_delay_waits_before_the_child(self):
        started = time.monotonic()
        assert _run('<Delay delay_msec="50"><AlwaysSuccess/></Delay>', tick_rate_hz=100)["success"]
        assert time.monotonic() - started >= 0.05

    def test_run_once_skips_after_the_first_run(self):
        r = _run('<Sequence><Script code="n := 0"/><Repeat num_cycles="3"><RunOnce><Script code="n += 1"/></RunOnce>'
                 '</Repeat></Sequence>')
        assert r["blackboard"]["n"] == 1

    def test_precondition(self):
        assert _run('<Precondition if="x &gt; 1" else="SUCCESS"><AlwaysFailure/></Precondition>',
                    blackboard={"x": 0})["success"] is True
        assert _run('<Precondition if="x &gt; 1"><AlwaysSuccess/></Precondition>',
                    blackboard={"x": 0})["outcome"] == "failed"
        assert _run('<Precondition if="x &gt; 1"><AlwaysSuccess/></Precondition>', blackboard={"x": 5})["success"]

    def test_loops_write_each_item(self):
        r = _run('<LoopInt queue="1;2;3" value="{item}"><Script code="total := total + item"/></LoopInt>',
                 blackboard={"total": 0})
        assert r["blackboard"]["total"] == 6
        r = _run('<LoopString queue="{rooms}" value="{room}"><Script code="last := room"/></LoopString>',
                 blackboard={"rooms": ["a", "b"]})
        assert r["blackboard"]["last"] == "b"
        assert _run('<LoopInt queue="" if_empty="FAILURE"><AlwaysSuccess/></LoopInt>')["outcome"] == "failed"


class TestControls:
    def test_if_then_else(self):
        xml = ('<IfThenElse><ScriptCondition code="a == 1"/><SetBlackboard output_key="r" value="then"/>'
               '<SetBlackboard output_key="r" value="else"/></IfThenElse>')
        assert _run(xml, blackboard={"a": 1})["blackboard"]["r"] == "then"
        assert _run(xml, blackboard={"a": 2})["blackboard"]["r"] == "else"

    def test_while_do_else(self):
        xml = ('<WhileDoElse><ScriptCondition code="go"/><SetBlackboard output_key="r" value="do"/>'
               '<SetBlackboard output_key="r" value="else"/></WhileDoElse>')
        assert _run(xml, blackboard={"go": "true"})["blackboard"]["r"] == "do"
        assert _run(xml, blackboard={"go": "false"})["blackboard"]["r"] == "else"

    @pytest.mark.parametrize("mode,expected", [("a", "A"), ("b", "B"), ("zzz", "default")])
    def test_switch(self, mode, expected):
        xml = ('<Switch2 variable="{mode}" case_1="a" case_2="b"><SetBlackboard output_key="r" value="A"/>'
               '<SetBlackboard output_key="r" value="B"/><SetBlackboard output_key="r" value="default"/></Switch2>')
        assert _run(xml, blackboard={"mode": mode})["blackboard"]["r"] == expected

    def test_parallel_negative_counts_mean_all(self):
        assert _run('<Parallel success_count="-1"><AlwaysSuccess/><AlwaysSuccess/></Parallel>')["success"]
        assert _run('<Parallel success_count="-1" failure_count="-1"><AlwaysSuccess/><AlwaysFailure/></Parallel>',
                    )["outcome"] == "failed"

    def test_parallel_all(self):
        assert _run('<ParallelAll max_failures="2"><AlwaysFailure/><AlwaysSuccess/></ParallelAll>')["success"]
        assert _run('<ParallelAll max_failures="1"><AlwaysFailure/><AlwaysSuccess/></ParallelAll>')["outcome"] == "failed"

    def test_sequence_with_memory_resumes_at_the_failed_child(self):
        import py_trees
        bb = {"a": 0, "ok": False}
        root = tree_builder.build_tree(
            '<SequenceWithMemory><Script code="a := a + 1"/><ScriptCondition code="ok"/></SequenceWithMemory>', bb)
        tree = py_trees.trees.BehaviourTree(root)
        tree.tick()
        bb["ok"] = True
        tree.tick()
        assert root.status.name == "SUCCESS"
        assert bb["a"] == 1  # the first child didn't run again

    def test_async_variants_run_like_their_memory_versions(self):
        assert _run('<AsyncSequence><AlwaysSuccess/><AlwaysSuccess/></AsyncSequence>')["success"]
        assert _run('<AsyncFallback><AlwaysFailure/><AlwaysSuccess/></AsyncFallback>')["success"]


class TestSubTreesAndDocuments:
    def test_subtree_runs_with_port_remapping_both_ways(self):
        r = _run('<Sequence><SubTree ID="deliver" target="{room}" result="{outcome}"/></Sequence>',
                 blackboard={"room": "lab"},
                 subtrees={"deliver": '<Sequence><SetBlackboard output_key="result" value="{target}"/></Sequence>'})
        assert r["outcome"] == "success"
        assert r["blackboard"]["outcome"] == "lab"

    def test_subtree_found_through_the_resolver(self):
        r = _run('<SubTree ID="helper"/>', subtree_resolver=lambda name: "<AlwaysSuccess/>" if name == "helper" else "")
        assert r["success"]

    def test_missing_subtree_is_a_clear_error_before_running(self):
        r = _run('<Sequence><Script code="x := 1"/><SubTree ID="nope"/></Sequence>')
        assert r["outcome"] == "invalid_tree"
        assert "'nope'" in r["error"]
        assert r["blackboard"] == {}  # nothing ran

    def test_subtree_loop_is_rejected(self):
        r = _run('<SubTree ID="a"/>', subtrees={"a": '<SubTree ID="b"/>', "b": '<SubTree ID="a"/>'})
        assert r["outcome"] == "invalid_tree"
        assert "loop" in r["error"]

    def test_a_full_groot2_document_runs_its_main_tree(self):
        r = _run('<root BTCPP_format="4" main_tree_to_execute="Main"><BehaviorTree ID="Main"><Sequence>'
                 '<SubTree ID="Helper"/></Sequence></BehaviorTree><BehaviorTree ID="Helper">'
                 '<SetBlackboard output_key="done" value="yes"/></BehaviorTree><TreeNodesModel/></root>')
        assert r["success"]
        assert r["blackboard"]["done"] == "yes"


class TestValidation:
    def test_lists_every_problem_with_its_path(self):
        problems = tree_builder.validate_tree(
            '<Sequence name="main"><NotReal/><Inverter/><IfThenElse><AlwaysSuccess/></IfThenElse>'
            '<AlwaysSuccess><AlwaysSuccess/></AlwaysSuccess></Sequence>')
        errors = [p for p in problems if p["level"] == "error"]
        assert len(errors) == 4
        assert errors[0]["path"] == "Sequence(main) > NotReal"
        assert "exactly 1 child" in errors[1]["message"]
        assert "at least 2" in errors[2]["message"]
        assert "leaf" in errors[3]["message"]

    def test_manual_selector_says_why_it_is_unsupported(self):
        problems = tree_builder.validate_tree("<ManualSelector><AlwaysSuccess/></ManualSelector>")
        assert "person" in problems[0]["message"]

    def test_bad_conditions_and_empty_controls_are_warnings(self):
        problems = tree_builder.validate_tree('<Sequence><Sequence/><AlwaysSuccess _skipIf="a &amp;&amp;"/></Sequence>')
        assert {p["level"] for p in problems} == {"warning"}
        assert len(problems) == 2

    def test_an_invalid_tree_never_starts(self):
        r = _run('<Sequence><SetBlackboard output_key="ran" value="1"/><Bogus/></Sequence>')
        assert r["outcome"] == "invalid_tree"
        assert "ran" not in r["blackboard"]

    def test_build_node_errors_carry_the_path(self):
        with pytest.raises(tree_builder.TreeBuildError) as info:
            tree_builder.build_tree('<Sequence name="s"><Inverter/></Sequence>', {})
        assert info.value.path == "Sequence(s) > Inverter"

    # 2026-09-29 stress test finding F10 (LOW): validate_tree had no cap on
    # a single tree's own node count or plain XML nesting depth (distinct
    # from MAX_SUBTREE_DEPTH, which only bounds <SubTree> recursion) -- an
    # enormous or pathologically deep tree_xml in a RUN_TASK dispatch was
    # accepted with no limit at all.
    def test_a_tree_with_too_many_nodes_is_rejected(self):
        xml = "<Sequence>" + "<AlwaysSuccess/>" * (tree_builder.MAX_TREE_NODES + 10) + "</Sequence>"
        problems = tree_builder.validate_tree(xml)
        errors = [p for p in problems if p["level"] == "error"]
        assert len(errors) == 1
        assert "more than" in errors[0]["message"] and "nodes" in errors[0]["message"]

    def test_a_tree_under_the_node_cap_is_not_flagged(self):
        xml = "<Sequence>" + "<AlwaysSuccess/>" * (tree_builder.MAX_TREE_NODES - 10) + "</Sequence>"
        problems = tree_builder.validate_tree(xml)
        assert not [p for p in problems if p["level"] == "error"]

    def test_a_tree_nested_too_deeply_is_rejected(self):
        xml = "<AlwaysSuccess/>"
        for _ in range(tree_builder.MAX_TREE_DEPTH + 10):
            xml = f"<Inverter>{xml}</Inverter>"
        problems = tree_builder.validate_tree(xml)  # must not stack-overflow either
        errors = [p for p in problems if p["level"] == "error"]
        assert len(errors) == 1
        assert "nested more than" in errors[0]["message"]

    def test_a_tree_under_the_depth_cap_is_not_flagged_for_depth(self):
        xml = "<AlwaysSuccess/>"
        for _ in range(tree_builder.MAX_TREE_DEPTH - 10):
            xml = f"<Inverter>{xml}</Inverter>"
        problems = tree_builder.validate_tree(xml)
        assert not [p for p in problems if p["level"] == "error" and "nested more than" in p["message"]]

    def test_an_oversized_tree_never_starts(self):
        xml = "<Sequence>" + "<AlwaysSuccess/>" * (tree_builder.MAX_TREE_NODES + 10) + "</Sequence>"
        r = _run(xml)
        assert r["outcome"] == "invalid_tree"


class TestExecutionReport:
    def test_counts_ticks_per_node_and_in_total(self):
        r = _run('<Sequence name="main"><Wait name="w" seconds="0.05"/><AlwaysSuccess name="done"/></Sequence>',
                 tick_rate_hz=100)
        rows = {row["name"]: row for row in r["nodes"]}
        assert rows["w"]["ticks"] >= 2
        assert rows["done"]["ticks"] == 1
        assert rows["w"]["active_s"] >= 0.04
        assert r["stats"]["nodes_total"] == 3
        assert r["stats"]["nodes_ticked"] == 3
        assert r["stats"]["node_ticks"] == sum(row["ticks"] for row in r["nodes"])
        assert r["stats"]["tree_ticks"] >= 2
        assert [e[1] for e in r["timeline"]][-1] == "main"
        json.dumps(r)  # JSON-safe

    def test_skipped_and_never_reached_nodes(self):
        r = _run('<Sequence><AlwaysSuccess name="skipme" _skipIf="!enabled"/><AlwaysFailure name="stop"/>'
                 '<AlwaysSuccess name="later"/></Sequence>', blackboard={"enabled": False})
        rows = {row["name"]: row for row in r["nodes"]}
        assert rows["skipme"]["status"] == "SKIPPED"
        assert rows["skipme"]["conditions"] == {"_skipIf": "!enabled"}
        assert rows["later"]["status"] == "IDLE"
        assert r["stats"]["nodes_skipped"] == 1
        assert r["stats"]["nodes_never_reached"] == 1
        assert r["failed_node"]["name"] == "stop"

    def test_a_crashing_node_is_named_and_the_tree_is_halted(self):
        import py_trees
        from xparo.bt_engine.node_registry import NODE_REGISTRY

        class Boom(py_trees.behaviour.Behaviour):
            def update(self):
                raise RuntimeError("sensor unplugged")

        halted = []

        class Runner(py_trees.behaviour.Behaviour):
            def update(self):
                return py_trees.common.Status.RUNNING

            def terminate(self, new_status):
                halted.append(new_status)

        NODE_REGISTRY["_Boom"] = lambda name, attrs, bb, children, ros: Boom(name=name)
        NODE_REGISTRY["_Runner"] = lambda name, attrs, bb, children, ros: Runner(name=name)
        try:
            r = _run('<Parallel success_count="2"><_Runner name="motor"/><_Boom name="lidar"/></Parallel>')
        finally:
            NODE_REGISTRY.pop("_Boom")
            NODE_REGISTRY.pop("_Runner")
        assert r["outcome"] == "node_error"
        assert r["failed_node"]["name"] == "lidar"
        assert "sensor unplugged" in r["error"]
        assert "RuntimeError" in r["traceback"]
        assert py_trees.common.Status.INVALID in halted

    def test_time_limit_stops_the_run_and_names_what_was_running(self):
        started = time.monotonic()
        r = _run('<Sequence><Wait name="forever" seconds="30"/></Sequence>', timeout_s=0.2, tick_rate_hz=50)
        assert time.monotonic() - started < 2
        assert r["outcome"] == "timeout"
        assert r["failed_node"]["name"] == "forever"
        assert "time limit" in r["error"]

    def test_cancel_event_stops_the_run_promptly(self):
        event = threading.Event()
        threading.Timer(0.1, event.set).start()
        started = time.monotonic()
        r = _run('<Wait name="w" seconds="30"/>', cancel_event=event, tick_rate_hz=2)
        assert time.monotonic() - started < 1.5
        assert r["outcome"] == "cancelled"

    def test_live_updates_failing_never_break_the_run(self):
        engine = MagicMock()
        engine.add_live_update.side_effect = ConnectionError("socket closed")
        r = BehaviorTreeExecutor(node=None, engine=engine).run_with_trace('<AlwaysSuccess/>', tick_rate_hz=0)
        assert r["success"]


class TestExpressions:
    def test_bt_cpp_assignment_forms(self):
        bb = {}
        expr.run_script("a := 1; b = 2; a += 4; b *= 3", bb)
        assert bb == {"a": 5, "b": 6}

    def test_short_circuit_and_text_booleans(self):
        assert expr.evaluate_condition("ready && missing > 1", {"ready": False}) is False
        assert expr.evaluate_condition("!flag", {"flag": "false"}) is True
        assert expr.evaluate_condition("flag == true", {"flag": True}) is True

    def test_type_errors_become_expression_errors(self):
        with pytest.raises(expr.ExpressionError):
            expr.evaluate("'a' - 1", {})
        with pytest.raises(expr.ExpressionError):
            expr.evaluate("1 / 0", {})

    def test_tracked_blackboard_counts_writes(self):
        bb = TrackedBlackboard({"a": 1})
        bb["a"] = 2
        bb["b"] = 1
        del bb["b"]
        assert bb.versions == {"a": 2, "b": 2}


class TestRunTask:
    def _handle(self, val, **kwargs):
        responses, history = [], []
        kwargs.setdefault("xparo_stage", "development")
        run_task.handle_run_task(_executor(), {"task_id": "t", **val}, responses.append,
                                 add_task_history=history.append, on_started=responses.append, **kwargs)
        return responses, history

    def test_sends_started_then_result_and_saves_the_full_report(self):
        responses, history = self._handle({"tree_xml": '<Sequence><Script code="x := 2"/></Sequence>',
                                           "save_task_history": True, "task_title": "Greet",
                                           "tree_name": "greet", "timeout_s": 60})
        assert list(responses[0]) == ["TASK_STARTED"]
        result = responses[1]["TASK_RESULT"]
        assert result["outcome"] == "success"
        assert result["run_id"] == responses[0]["TASK_STARTED"]["run_id"]
        assert result["stats"]["nodes_total"] == 2
        output = history[0]["output_data"]
        assert output["report"]["nodes"][1]["tag"] == "Script"
        assert output["report"]["blackboard_diff"] == {"added": ["x"], "changed": [], "removed": []}
        assert history[0]["input_data"]["task_title"] == "Greet"
        assert history[0]["input_data"]["timeout_s"] == 60
        json.dumps(history[0])

    def test_clean_failure_keeps_error_empty_but_explains(self):
        responses, _ = self._handle({"tree_xml": '<Sequence><AlwaysFailure name="gate"/></Sequence>'})
        result = responses[-1]["TASK_RESULT"]
        assert result["error"] == ""
        assert result["failed_node"]["name"] == "gate"
        assert "gate" in result["explanation"]
        assert result["retryable"] is True

    def test_empty_tree_is_explained_and_not_retryable(self):
        responses, _ = self._handle({"tree_xml": "", "tree_name": "patrol"})
        result = responses[-1]["TASK_RESULT"]
        assert result["outcome"] == "invalid_tree"
        assert "'patrol'" in result["error"]
        assert result["retryable"] is False

    def test_timeout_from_the_task(self):
        responses, _ = self._handle({"tree_xml": '<Wait seconds="30"/>', "timeout_s": 0.2})
        assert responses[-1]["TASK_RESULT"]["outcome"] == "timeout"

    def test_cancel_task_stops_a_running_task(self):
        responses = []
        thread = threading.Thread(target=run_task.handle_run_task, args=(
            _executor(), {"task_id": "long", "tree_xml": '<Wait seconds="30"/>'}, responses.append),
            kwargs={"xparo_stage": "development"})
        thread.start()
        for _ in range(100):
            if run_task.active_runs():
                break
            time.sleep(0.01)
        assert run_task.active_runs()[0]["task_id"] == "long"
        assert run_task.cancel_task(task_id="long", reason="operator pressed stop")
        thread.join(3)
        assert not thread.is_alive()
        result = responses[-1]["TASK_RESULT"]
        assert result["outcome"] == "cancelled"
        assert "operator pressed stop" in result["explanation"]
        assert run_task.active_runs() == []

    def test_unserializable_blackboard_values_are_made_safe(self):
        import py_trees
        from xparo.bt_engine.node_registry import NODE_REGISTRY

        class Store(py_trees.behaviour.Behaviour):
            def __init__(self, name, bb):
                super().__init__(name=name)
                self.bb = bb

            def update(self):
                self.bb["obj"] = object()
                return py_trees.common.Status.SUCCESS

        NODE_REGISTRY["_Store"] = lambda name, attrs, bb, children, ros: Store(name, bb)
        try:
            responses, history = self._handle({"tree_xml": "<_Store/>", "save_task_history": True})
        finally:
            NODE_REGISTRY.pop("_Store")
        json.dumps(history[0])
        assert history[0]["output_data"]["blackboard"]["obj"].startswith("<object")


class TestEngineCancel:
    def test_cancel_task_message_acks_whether_or_not_it_was_running(self):
        from xparo.engine import Engine
        engine = Engine("secret", "proj-cancel-test", connection_type="offline")
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(json.loads(message))
        engine.on_ws_message('ws', {"CANCEL_TASK": {"task_id": "nothing-running"}})
        ack = [m["TASK_CANCEL_ACK"] for m in sent if "TASK_CANCEL_ACK" in m][0]
        assert ack["found"] is False
        assert "isn't running" in ack["message"]
