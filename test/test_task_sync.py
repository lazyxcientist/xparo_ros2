"""Local task execution: a task synced from Django (apps/analytics/
data_analyis.py's DataAnalysis._get_custom_tasks) can be triggered
directly on this robot -- publishing its task_id on /xparo/run_task
(xparo_ros.py) -- resolved entirely from this robot's own already-synced
local files, no Django round trip needed at trigger time. Covers
task_sync.py's pure resolution functions plus Engine.sync_custom_tasks/
run_task_from_topic and on_ws_message's "custom_tasks" sync branch --
mirrors test_rosbag_config_sync.py's own structure (pure loader tests,
then Engine-level sync tests) and test_run_task.py's on_ws_message-round-
trip style for the trigger tests.
"""
import json
import time
from unittest.mock import MagicMock

from xparo import sync_hash
from xparo.bt_engine import task_sync, tree_builder
from xparo.bt_engine.executor import BehaviorTreeExecutor
from xparo.bt_engine.xml_parser import tree_file_content


def _files(tmp_path):
    behaviors = tmp_path / "custom_behaviors"
    envs = tmp_path / "custom_envs"
    behaviors.mkdir()
    envs.mkdir()
    return {
        "behavior": str(tmp_path / "default.xml"),
        "xparo_custom_behaviors_folder_path": str(behaviors),
        "xparo_custom_evns_folder_path": str(envs),
    }


class TestLoadCustomTasks:
    def test_no_persisted_file_returns_an_empty_dict(self, tmp_path):
        assert task_sync.load_custom_tasks(str(tmp_path)) == {}

    def test_loads_whatever_was_persisted(self, tmp_path):
        (tmp_path / task_sync.TASKS_FILENAME).write_text(json.dumps({"t1": {"behaviour_tree_name": ""}}))
        assert task_sync.load_custom_tasks(str(tmp_path)) == {"t1": {"behaviour_tree_name": ""}}

    def test_a_corrupt_file_falls_back_to_empty_rather_than_raising(self, tmp_path):
        (tmp_path / task_sync.TASKS_FILENAME).write_text("not json")
        assert task_sync.load_custom_tasks(str(tmp_path)) == {}


class TestResolveTreeXml:
    def test_empty_name_reads_the_default_behavior_file(self, tmp_path):
        files = _files(tmp_path)
        with open(files["behavior"], "w") as f:
            f.write('<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
                    '<BehaviorTree ID="MainTree">\n<LoadNextDelivery />\n</BehaviorTree>\n</root>')

        assert task_sync.resolve_tree_xml("", files).strip() == "<LoadNextDelivery />"

    def test_a_name_reads_the_matching_custom_aiml_file(self, tmp_path):
        # custom_aiml/<name>.xml, not flat under custom_behaviors/ --
        # matches engine.py's own on_ws_message "custom_aiml" branch and
        # get_local_file_content, which both read/write this exact
        # subfolder (see this feature's own directory-restructure fix for
        # why: a flat shared folder let a custom_aiml sync silently
        # overwrite an unrelated curated fixture with the same name).
        files = _files(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir()
        pth = aiml_dir / "quick_delivery_tree.xml"
        pth.write_text('<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
                        '<BehaviorTree ID="MainTree">\n<PlayAudio file_path="/x.mp3" />\n</BehaviorTree>\n</root>')

        result = task_sync.resolve_tree_xml("quick_delivery_tree", files)

        assert result.strip() == '<PlayAudio file_path="/x.mp3" />'

    def test_a_file_still_sitting_at_the_old_flat_location_is_not_found(self, tmp_path):
        """Regression guard for the exact bug this fix closes: a task
        whose tree was actually synced through the custom_aiml mechanism
        must never silently fall back to nothing just because an old,
        stale copy happens to sit one level up."""
        files = _files(tmp_path)
        (tmp_path / "custom_behaviors" / "quick_delivery_tree.xml").write_text(
            '<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
            '<BehaviorTree ID="MainTree">\n<PlayAudio file_path="/x.mp3" />\n</BehaviorTree>\n</root>'
        )

        assert task_sync.resolve_tree_xml("quick_delivery_tree", files) == ""

    def test_missing_file_returns_empty_string_not_an_exception(self, tmp_path):
        files = _files(tmp_path)
        assert task_sync.resolve_tree_xml("never_synced", files) == ""

    def test_content_without_the_wrapper_markers_is_returned_as_is(self, tmp_path):
        """Falls back gracefully (matching engine.py's own get_local_files
        fallback) rather than failing a task run over a formatting quirk
        in an unrelated sync path."""
        files = _files(tmp_path)
        with open(files["behavior"], "w") as f:
            f.write("<LoadNextDelivery />")

        assert task_sync.resolve_tree_xml("", files) == "<LoadNextDelivery />"


class TestResolveBlackboard:
    def test_default_mapping_uses_the_literal_value(self, tmp_path):
        files = _files(tmp_path)
        mapping = {"dock_name": {"mapping_type": "default", "value": "dock-1"}}
        assert task_sync.resolve_blackboard(mapping, [], {}, files) == {"dock_name": "dock-1"}

    def test_env_mapping_reads_the_synced_env_file(self, tmp_path):
        files = _files(tmp_path)
        (tmp_path / "custom_envs" / "robot.env").write_text("DOCK_NAME=dock-42\n")
        mapping = {"dock_name": {"mapping_type": "env", "env_file": "robot", "env_var": "DOCK_NAME"}}

        assert task_sync.resolve_blackboard(mapping, [], {}, files) == {"dock_name": "dock-42"}

    def test_env_mapping_missing_the_file_leaves_the_var_unresolved(self, tmp_path):
        files = _files(tmp_path)
        mapping = {"dock_name": {"mapping_type": "env", "env_file": "nope", "env_var": "DOCK_NAME"}}
        assert task_sync.resolve_blackboard(mapping, [], {}, files) == {}

    def test_env_mapping_reads_the_env_synced_into_custom_maps(self, tmp_path):
        """ROSAPI-14: engine.py syncs environments into custom_envs/
        custom_maps/, but this looked in custom_envs/ -- 'From env' inputs
        were empty on every robot-started run."""
        files = _files(tmp_path)
        (tmp_path / "custom_envs" / "custom_maps").mkdir()
        (tmp_path / "custom_envs" / "custom_maps" / "ravi_site.env").write_text("SITE_GOAL=ward_env7\n")
        mapping = {"dock_name": {"mapping_type": "env", "env_file": "ravi_site", "env_var": "SITE_GOAL"}}

        assert task_sync.resolve_blackboard(mapping, [], {}, files) == {"dock_name": "ward_env7"}

    def test_an_unresolved_env_input_is_reported(self, tmp_path):
        files = _files(tmp_path)
        (tmp_path / "custom_envs" / "custom_maps").mkdir()
        (tmp_path / "custom_envs" / "custom_maps" / "ravi_site.env").write_text("OTHER=1\n")
        mapping = {
            "dock_name": {"mapping_type": "env", "env_file": "ravi_site", "env_var": "SITE_GOAL"},
            "bay": {"mapping_type": "env", "env_file": "gone", "env_var": "BAY"},
        }
        problems = []

        assert task_sync.resolve_blackboard(mapping, [], {}, files, problems) == {}
        assert len(problems) == 2
        assert "'dock_name'" in problems[0] and "'SITE_GOAL' isn't set" in problems[0]
        assert "'bay'" in problems[1] and "no environment 'gone'" in problems[1]

    def test_task_param_mapping_uses_the_params_default(self, tmp_path):
        files = _files(tmp_path)
        mapping = {"speed": {"mapping_type": "task_param", "param_id": "p1"}}
        params = [{"id": "p1", "name": "max_speed", "default_value": "1.5"}]

        assert task_sync.resolve_blackboard(mapping, params, {}, files) == {"speed": "1.5"}

    def test_task_param_mapping_is_overridden_when_provided(self, tmp_path):
        files = _files(tmp_path)
        mapping = {"speed": {"mapping_type": "task_param", "param_id": "p1"}}
        params = [{"id": "p1", "name": "max_speed", "default_value": "1.5"}]

        result = task_sync.resolve_blackboard(mapping, params, {"max_speed": "3.0"}, files)

        assert result == {"speed": "3.0"}

    def test_random_number_resolves_within_range(self, tmp_path):
        files = _files(tmp_path)
        mapping = {"n": {"mapping_type": "random_number", "min": 5, "max": 5}}
        assert task_sync.resolve_blackboard(mapping, [], {}, files) == {"n": 5}

    def test_random_string_resolves_to_the_requested_length(self, tmp_path):
        files = _files(tmp_path)
        mapping = {"s": {"mapping_type": "random_string", "length": 12}}
        result = task_sync.resolve_blackboard(mapping, [], {}, files)
        assert len(result["s"]) == 12

    def test_random_values_are_generated_fresh_each_call_not_cached(self, tmp_path):
        files = _files(tmp_path)
        mapping = {"s": {"mapping_type": "random_string", "length": 16}}
        first = task_sync.resolve_blackboard(mapping, [], {}, files)["s"]
        second = task_sync.resolve_blackboard(mapping, [], {}, files)["s"]
        assert first != second


class TestBuildRunTaskVal:
    def test_returns_none_when_task_id_is_not_in_the_local_cache(self, tmp_path):
        files = _files(tmp_path)
        assert task_sync.build_run_task_val("missing", None, {}, files) is None

    def test_builds_the_exact_shape_handle_run_task_expects(self, tmp_path):
        files = _files(tmp_path)
        with open(files["behavior"], "w") as f:
            f.write('<BehaviorTree ID="MainTree"><DockRobot dock="{dock_name}" /></BehaviorTree>')
        custom_tasks = {"t1": {
            "behaviour_tree_name": "", "save_task_history": True,
            "blackboard_mapping": {"dock_name": {"mapping_type": "default", "value": "dock-1"}},
            "params": [],
        }}

        val = task_sync.build_run_task_val("t1", None, custom_tasks, files)

        assert val == {
            "task_id": "t1",
            "tree_xml": '<DockRobot dock="{dock_name}" />',
            "blackboard": {"dock_name": "dock-1"},
            "save_task_history": True,
            # No "stage" key on the cached task -- defaults to
            # "development", the least-trusted reading (see
            # build_run_task_val's own comment).
            "stage": "development",
            # Not in this (older) cache either: handle_run_task falls back
            # to its default time limit; title/tree name label the report.
            "timeout_s": None,
            "task_title": "",
            "tree_name": "",
            "trigger": "ros_topic",
            "input_problems": [],
            "ignored_inputs": [],
            "ignored_overrides": [],
            # Stage versions: an older cache has none of these.
            "tree_stage": "", "tree_version": "", "tree_version_at": "", "tree_error": "",
            "tree_hash": sync_hash.content_hash('<DockRobot dock="{dock_name}" />'),
        }

    def test_an_ignored_override_name_is_cut_short(self, tmp_path):
        """KUNAL-25's ignored names go into the result and the task history;
        only override values were size-checked, so a 1 MB key went up whole."""
        files = _files(tmp_path)
        with open(files["behavior"], "w") as f:
            f.write('<BehaviorTree ID="MainTree"><Sequence><SetBlackboard output_key="a" value="{goal}" />'
                    '</Sequence></BehaviorTree>')
        custom_tasks = {"t1": {"behaviour_tree_name": "", "blackboard_mapping": {},
                               "params": [{"id": "p1", "name": "goal", "default_value": ""}]}}

        val = task_sync.build_run_task_val("t1", {"goal": "w3", "k" * 1_000_000: 1, "destination": "x"},
                                           custom_tasks, files)

        assert val["ignored_overrides"] == ["destination", "k" * task_sync.IGNORED_OVERRIDE_NAME_CHARS]

    def _write_tree(self, files, name, body):
        import pathlib
        folder = pathlib.Path(files["xparo_custom_behaviors_folder_path"]) / "custom_aiml"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.xml").write_text(tree_file_content(body))

    def test_a_mapping_for_an_input_the_tree_no_longer_has_is_ignored(self, tmp_path):
        """ROSAPI-17: the tree lost {dock_name}, the synced task kept its
        From-env mapping, and with the environment gone every run was
        refused for an input nothing reads (and reports listed it)."""
        files = _files(tmp_path)
        self._write_tree(files, "ravi_ctrl_a", '<Sequence><Timeout msec="500"><Wait seconds="2"/></Timeout></Sequence>')
        custom_tasks = {"t1": {"title": "Ravi deliver", "behaviour_tree_name": "ravi_ctrl_a", "params": [],
                               "blackboard_mapping": {"dock_name": {"mapping_type": "env", "env_file": "ravi_site",
                                                                    "env_var": "SITE_GOAL"}}}}

        val = task_sync.build_run_task_val("t1", None, custom_tasks, files)

        assert val["input_problems"] == []
        assert val["blackboard"] == {}
        assert val["ignored_inputs"] == ["dock_name"]

    def test_an_input_the_tree_reads_in_any_way_stays_required(self, tmp_path):
        files = _files(tmp_path)
        self._write_tree(files, "inner", '<NavigateTo goal="{x}"/>')
        mapping = {"x": {"mapping_type": "env", "env_file": "ravi_site", "env_var": "SITE_GOAL"}}
        for body in ('<NavigateTo goal="{x}"/>', '<Script code="y := x + 1"/>', '<AlwaysSuccess _skipIf="x == 1"/>',
                     '<WasEntryUpdated entry="x"/>', '<SubTree ID="inner"/>'):
            self._write_tree(files, "outer", body)
            val = task_sync.build_run_task_val(
                "t1", None, {"t1": {"behaviour_tree_name": "outer", "params": [], "blackboard_mapping": mapping}}, files)
            assert val["ignored_inputs"] == [], body
            assert len(val["input_problems"]) == 1 and "'x'" in val["input_problems"][0], body

    def test_a_tree_that_cant_be_read_keeps_every_mapping(self, tmp_path):
        files = _files(tmp_path)
        custom_tasks = {"t1": {"behaviour_tree_name": "not_synced_yet", "params": [],
                               "blackboard_mapping": {"dock_name": {"mapping_type": "default", "value": "d1"}}}}
        val = task_sync.build_run_task_val("t1", None, custom_tasks, files)
        assert val["blackboard"] == {"dock_name": "d1"} and val["ignored_inputs"] == []


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-task-sync-test", **kwargs)
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    # Engine.__init__ points this at the real checkout's config/default.xml
    # unconditionally -- tests in this file that write to it (resolving a
    # ''-named task's tree_xml) must redirect it too, or they clobber
    # whatever real content is actually synced there.
    engine.files["behavior"] = str(tmp_path / "default.xml")
    return engine


class TestEngineSyncCustomTasks:
    def test_persists_the_tasks_to_the_expected_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        tasks = {"t1": {"behaviour_tree_name": "", "blackboard_mapping": {}, "params": [], "save_task_history": True}}

        engine.sync_custom_tasks(tasks)

        persisted = json.loads((tmp_path / task_sync.TASKS_FILENAME).read_text())
        assert persisted == tasks
        assert task_sync.load_custom_tasks(str(tmp_path)) == tasks


class TestRunTaskFromTopic:
    def test_no_bt_executor_is_a_safe_noop(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine.bt_executor is None
        engine.run_task_from_topic("t1", {})  # must not raise

    def test_unknown_task_id_is_a_safe_noop(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)
        engine.sync_custom_tasks({})

        engine.run_task_from_topic("does-not-exist", {})  # must not raise

    def test_a_known_task_id_actually_runs_and_reports_task_result(self, tmp_path):
        # development runs any task stage -- this test is about the
        # dispatch plumbing, not stage gating (run_task.py has its own
        # dedicated tests for that).
        engine = _make_engine(tmp_path, xparo_stage="development")
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(message)
        engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)
        engine.sync_custom_tasks({"t1": {
            "behaviour_tree_name": "", "save_task_history": False,
            "blackboard_mapping": {}, "params": [],
        }})
        with open(engine.files["behavior"], "w") as f:
            f.write('<BehaviorTree ID="MainTree"><LoadNextDelivery /></BehaviorTree>')

        engine.run_task_from_topic("t1", {})

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
        assert task_result["task_id"] == "t1"
        assert task_result["success"] is True

    def test_override_params_reach_the_resolved_blackboard(self, tmp_path):
        engine = _make_engine(tmp_path, xparo_stage="development")
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(message)
        engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)
        engine.sync_custom_tasks({"t1": {
            "behaviour_tree_name": "", "save_task_history": True,
            "blackboard_mapping": {"speed": {"mapping_type": "task_param", "param_id": "p1"}},
            "params": [{"id": "p1", "name": "max_speed", "default_value": "1.0"}],
        }})
        with open(engine.files["behavior"], "w") as f:
            f.write('<BehaviorTree ID="MainTree"><SetBlackboard output_key="used" value="{speed}" /></BehaviorTree>')

        history = []
        engine.add_task_history = history.append
        engine.run_task_from_topic("t1", {"max_speed": "9.9"})

        deadline = time.monotonic() + 2
        while not history and time.monotonic() < deadline:
            time.sleep(0.02)

        assert history, "add_task_history was never called"
        assert history[0]["input_data"]["blackboard"]["speed"] == "9.9"


class TestOnWsMessageCustomTasksSync:
    def test_custom_tasks_key_dispatches_to_sync_custom_tasks(self, tmp_path):
        engine = _make_engine(tmp_path)
        tasks = {"t1": {"behaviour_tree_name": "", "blackboard_mapping": {}, "params": [], "save_task_history": True}}

        engine.on_ws_message('ws', {"custom_tasks": tasks})

        assert task_sync.load_custom_tasks(str(tmp_path)) == tasks


class TestRunTaskTopicSafety:
    """Persona field test (2026-10-14): one /xparo/run_task publish ran the
    delivery on every robot sharing the ROS domain, and a kiosk double tap
    ran the same delivery twice at once."""

    def test_a_named_robot_only(self, tmp_path):
        import socket
        engine = _make_engine(tmp_path)
        assert engine.is_this_robot(None)
        assert engine.is_this_robot(engine.local_database.unique_id)
        assert engine.is_this_robot(socket.gethostname())
        assert not engine.is_this_robot("SB-0002-somewhere-else")

    def test_the_same_task_twice_is_refused_while_it_runs(self, tmp_path):
        from xparo.bt_engine import run_task
        engine = _make_engine(tmp_path, xparo_stage="development")
        finished = []
        engine._task_finished = finished.append
        engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)
        import threading
        with run_task._active_lock:
            run_task._active_runs["already"] = {"task_id": "t1", "event": threading.Event(), "started": time.monotonic(), "reason": ""}
        try:
            engine.run_task_from_topic("t1", {})
        finally:
            with run_task._active_lock:
                run_task._active_runs.pop("already", None)
        assert finished and finished[0]["TASK_RESULT"]["outcome"] == "busy"
        assert finished[0]["TASK_RESULT"]["retryable"] is False


# ---- 2026-10-31 field test: ROSAPI-6/8/10, RO-15 ---------------------------

class TestParseRunTaskMessage:
    def test_json_with_task_id(self):
        assert task_sync.parse_run_task_message('{"task_id": "t1", "override_params": {"goal": "w3"}}') == (
            {"task_id": "t1", "override_params": {"goal": "w3"}}, "")

    def test_a_bare_task_id_is_accepted(self):
        """RO-15: the docs prose says "publishing a task's ID runs it"."""
        assert task_sync.parse_run_task_message("50a12826-aaaa") == ({"task_id": "50a12826-aaaa"}, "")

    def test_bad_json_says_what_was_expected(self):
        payload, problem = task_sync.parse_run_task_message('{task_id: oops')
        assert payload is None
        assert 'expected JSON {"task_id"' in problem

    def test_a_json_list_is_rejected(self):
        payload, problem = task_sync.parse_run_task_message('["t1"]')
        assert payload is None and "not an object" in problem


class TestRunTaskMessageFieldTypes:
    """ROSAPI-13: "robot": ["a"] got no answer at all (read as another
    robot), "allow_parallel": "yes" counted as true."""

    def test_a_robot_that_is_not_a_string_is_a_problem(self):
        payload, problem = task_sync.parse_run_task_message('{"task_id": "t1", "robot": ["a"]}')
        assert payload["task_id"] == "t1"
        assert '"robot" must be a string' in problem

    def test_allow_parallel_must_be_a_boolean(self):
        _, problem = task_sync.parse_run_task_message('{"task_id": "t1", "allow_parallel": "yes"}')
        assert '"allow_parallel" must be true or false, not "yes"' in problem
        assert task_sync.parse_run_task_message('{"task_id": "t1", "allow_parallel": true}')[1] == ""


class TestValidateRunTaskRequest:
    def test_a_good_request_passes(self):
        assert task_sync.validate_run_task_request("t1", {"goal": "ward_3", "n": 2}) == ""
        assert task_sync.validate_run_task_request("t1", None) == ""

    def test_null_or_missing_task_id(self):
        for task_id in (None, "", "   "):
            assert 'missing "task_id"' in task_sync.validate_run_task_request(task_id, {})

    def test_a_task_id_of_the_wrong_type_says_so(self):
        """ROSAPI-13: 12345 or a list answered 'missing "task_id"'."""
        for task_id, kind in ((12345, "int"), (["t1"], "list")):
            problem = task_sync.validate_run_task_request(task_id, {})
            assert f'"task_id" must be a string' in problem and kind in problem

    def test_override_params_must_be_an_object(self):
        """ROSAPI-6: a list here raised AttributeError inside
        resolve_blackboard and took the whole node down."""
        problem = task_sync.validate_run_task_request("t1", ["x"])
        assert "override_params" in problem and "list" in problem

    def test_oversize_values_are_rejected(self):
        """ROSAPI-10: a 2 MB value ran and was stored twice in the history."""
        problem = task_sync.validate_run_task_request("t1", {"goal": "W" * 2_000_000})
        assert "goal" in problem and str(task_sync.MAX_OVERRIDE_VALUE_CHARS) in problem

    def test_too_many_entries_are_rejected(self):
        too_many = {f"p{i}": "x" for i in range(task_sync.MAX_OVERRIDE_PARAMS + 1)}
        assert "entries" in task_sync.validate_run_task_request("t1", too_many)


class TestRunTaskFromTopicRejections:
    """ROSAPI-6/8: a malformed request must neither raise (rclpy.spin would
    end the node) nor go unanswered -- invalid_request goes out on
    /xparo/task_result through on_task_event, and not up to the dashboard."""

    def _engine_with_events(self, tmp_path):
        engine = _make_engine(tmp_path, xparo_stage="development")
        engine.bt_executor = BehaviorTreeExecutor(node=MagicMock(), engine=engine)
        engine.sync_custom_tasks({"t1": {
            "behaviour_tree_name": "", "save_task_history": False,
            "blackboard_mapping": {"goal": {"mapping_type": "task_param", "param_id": "p1"}},
            "params": [{"id": "p1", "name": "goal", "default_value": "a"}],
        }})
        events = []
        engine.on_task_event = lambda kind, info: events.append((kind, info))
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(message)
        return engine, events, sent

    def test_a_list_for_override_params_answers_invalid_request_instead_of_raising(self, tmp_path):
        engine, events, sent = self._engine_with_events(tmp_path)

        engine.run_task_from_topic("t1", ["x"])  # used to raise: 'list' object has no attribute 'get'

        assert [(kind, info["outcome"]) for kind, info in events] == [("finished", "invalid_request")]
        assert events[0][1]["task_id"] == "t1"
        assert events[0][1]["retryable"] is False
        assert "override_params" in events[0][1]["error"]
        assert not any("TASK_RESULT" in raw for raw in sent)  # nothing went up to the dashboard

    def test_a_from_env_input_this_robot_cant_fill_refuses_the_run(self, tmp_path):
        """ROSAPI-14: the run started without dock_name and reported
        success; it is refused with the reason instead."""
        engine, events, sent = self._engine_with_events(tmp_path)
        engine.files["xparo_custom_evns_folder_path"] = str(tmp_path / "custom_envs")
        engine.sync_custom_tasks({"t2": {
            "title": "Ravi deliver", "behaviour_tree_name": "", "save_task_history": False,
            "blackboard_mapping": {"dock_name": {"mapping_type": "env", "env_file": "ravi_site", "env_var": "SITE_GOAL"}},
            "params": [],
        }})
        started = []
        engine._start_task_thread = started.append

        engine.run_task_from_topic("t2", {})

        assert started == []
        assert [(kind, info["outcome"]) for kind, info in events] == [("finished", "invalid_request")]
        assert "'dock_name'" in events[0][1]["error"] and "ravi_site" in events[0][1]["error"]

        engine.on_ws_message('ws', {"custom_maps": {"ravi_site": "SITE_GOAL=ward_env7\n"}})
        engine.run_task_from_topic("t2", {})

        assert started and started[0]["blackboard"] == {"dock_name": "ward_env7"}

    def test_a_stale_mapping_never_blocks_a_run_and_warns_once(self, tmp_path):
        """ROSAPI-17: a From-env mapping left over from an earlier version
        of the tree refused every run once its environment was gone."""
        engine, events, sent = self._engine_with_events(tmp_path)
        engine.files["xparo_custom_evns_folder_path"] = str(tmp_path / "custom_envs")
        engine.on_ws_message('ws', {"custom_aiml": {"ravi_ctrl_a": '<Sequence><Wait seconds="0"/></Sequence>'}})
        engine.sync_custom_tasks({"t2": {
            "title": "Ravi deliver", "behaviour_tree_name": "ravi_ctrl_a", "save_task_history": False,
            "blackboard_mapping": {"dock_name": {"mapping_type": "env", "env_file": "ravi_site", "env_var": "SITE_GOAL"}},
            "params": [],
        }})
        started = []
        engine._start_task_thread = started.append

        engine.run_task_from_topic("t2", {})
        engine.run_task_from_topic("t2", {})

        assert events == []
        assert len(started) == 2 and started[0]["blackboard"] == {}
        warnings = [c.args[0] for c in engine.bt_executor.node.get_logger.return_value.warning.call_args_list]
        assert len(warnings) == 1
        assert "Ravi deliver" in warnings[0] and "'dock_name'" in warnings[0]

    def test_a_missing_subtree_is_not_reported_as_an_unread_mapping(self, tmp_path):
        """R3 review: the SubTree that reads dock_name isn't on the robot, so
        the WARN said "the tree doesn't read it" -- the run then failed with
        invalid_tree naming the SubTree, the real cause."""
        engine, events, sent = self._engine_with_events(tmp_path)
        engine.files["xparo_custom_evns_folder_path"] = str(tmp_path / "custom_envs")
        engine.on_ws_message('ws', {"custom_aiml": {"ravi_ctrl_b": '<Sequence><SubTree ID="ravi_dock"/></Sequence>'}})
        engine.sync_custom_tasks({"t3": {
            "title": "Ravi dock", "behaviour_tree_name": "ravi_ctrl_b", "save_task_history": False,
            "blackboard_mapping": {"dock_name": {"mapping_type": "env", "env_file": "ravi_site", "env_var": "SITE_GOAL"}},
            "params": [],
        }})
        started = []
        engine._start_task_thread = started.append

        engine.run_task_from_topic("t3", {})

        assert engine.bt_executor.node.get_logger.return_value.warning.call_args_list == []
        assert events == [] and started[0]["ignored_inputs"] == []  # on to the tree check, which names ravi_dock
        problems = tree_builder.validate_tree(started[0]["tree_xml"], subtree_resolver=lambda tree_id: "")
        assert any("'ravi_dock'" in problem["message"] for problem in problems)

    def test_an_override_the_task_has_no_param_for_is_named_in_the_result(self, tmp_path):
        """KUNAL-25: override_params {"destination": "ward_3"} on a task
        without that param ran with destination unset and failed on it, with
        no word that the override reached nothing."""
        engine, events, sent = self._engine_with_events(tmp_path)
        with open(engine.files["behavior"], "w") as f:
            f.write('<BehaviorTree ID="MainTree"><Sequence><SetBlackboard output_key="a" value="{goal}" />'
                    '<SetBlackboard output_key="b" value="{destination}" /></Sequence></BehaviorTree>')

        def finished_result():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                done = [info for kind, info in events if kind == "finished"]
                if done:
                    events.clear()
                    return done[0]
                time.sleep(0.02)
            raise AssertionError(f"no task result; events={events}")

        engine.run_task_from_topic("t1", {"goal": "w3", "destination": "ward_3"})
        result = finished_result()

        assert result["ignored_override_params"] == ["destination"]
        assert "override_params 'destination' was ignored" in result["explanation"]
        assert "'goal'" not in result["explanation"]
        warnings = [c.args[0] for c in engine.bt_executor.node.get_logger.return_value.warning.call_args_list]
        assert any("'destination'" in w and "no param" in w for w in warnings)

        engine.run_task_from_topic("t1", {"goal": "w3"})  # only the task's own params: nothing to say
        result = finished_result()
        assert "ignored_override_params" not in result and "override_params" not in result["explanation"]

    def test_a_null_task_id_answers_invalid_request(self, tmp_path):
        engine, events, _ = self._engine_with_events(tmp_path)
        engine.run_task_from_topic(None, {})
        assert events[0][1]["outcome"] == "invalid_request" and "task_id" in events[0][1]["error"]

    def test_an_oversize_override_value_never_runs(self, tmp_path):
        engine, events, _ = self._engine_with_events(tmp_path)
        engine.run_task_from_topic("t1", {"goal": "W" * 2_000_000})
        assert [(kind, info["outcome"]) for kind, info in events] == [("finished", "invalid_request")]

    def test_reject_task_request_is_what_the_node_uses_for_unparseable_messages(self, tmp_path):
        engine, events, _ = self._engine_with_events(tmp_path)
        engine.reject_task_request(None, "could not parse the message as JSON")
        assert len(events) == 1
        assert events[0][1]["outcome"] == "invalid_request" and events[0][1]["task_id"] is None
        assert events[0][1]["error"] == "could not parse the message as JSON"


class TestStageVersionFields:
    """The dashboard marks saved versions of a tree for a stage (History ->
    Stages, apps/analytics/bt_versions.py). A task whose stage is marked
    names the version's hidden copy ".xpv.<key>" (pushed in custom_aiml with
    ".xpv.<key>.<SubTree>" copies) and carries tree_files hashes; tree_name
    stays the file, for Task history."""

    KEY = "3f9a0c1b7d22"

    def _write_tree(self, files, name, body):
        import pathlib
        folder = pathlib.Path(files["xparo_custom_behaviors_folder_path"]) / "custom_aiml"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.xml").write_text(tree_file_content(body))

    def _marked(self, files, main_body=None, sub_body='<LoadNextDelivery />'):
        main, sub = f".xpv.{self.KEY}", f".xpv.{self.KEY}.dock_seq"
        main_body = main_body or f'<Sequence><SubTree ID="{sub}"/></Sequence>'
        self._write_tree(files, main, main_body)
        self._write_tree(files, sub, sub_body)
        # the working copy on disk is different: it must never be used
        self._write_tree(files, "dock_seq", "<AlwaysFailure/>")
        return {"t1": {"behaviour_tree_name": main, "tree_name": "patrol", "stage": "production",
                       "tree_stage": "production", "tree_version": "v-1", "tree_version_at": "2026-11-02T14:05:00Z",
                       "tree_hash": "stored-hash",
                       "tree_files": {main: sync_hash.content_hash(main_body),
                                      sub: sync_hash.content_hash(sub_body)}}}

    def test_old_cache_without_new_keys_still_builds(self, tmp_path):
        files = _files(tmp_path)
        self._write_tree(files, "patrol", "<Wait seconds=\"1\"/>")
        val = task_sync.build_run_task_val("t1", None, {"t1": {"behaviour_tree_name": "patrol"}}, files)
        assert val["tree_xml"].strip() == '<Wait seconds="1"/>'
        assert (val["tree_name"], val["tree_stage"], val["tree_version"], val["tree_error"]) == ("patrol", "", "", "")
        assert "behaviour" not in val and "verified_bundle" not in val

    def test_a_marked_version_loads_its_hidden_copies_and_names_the_file(self, tmp_path):
        files = _files(tmp_path)
        cached = self._marked(files)
        val = task_sync.build_run_task_val("t1", None, cached, files)
        assert val["tree_error"] == ""
        assert val["tree_xml"] == f'<Sequence><SubTree ID=".xpv.{self.KEY}.dock_seq"/></Sequence>'
        assert val["subtrees"] == {f".xpv.{self.KEY}.dock_seq": "<LoadNextDelivery />"}
        assert val["verified_bundle"] is True
        assert (val["tree_name"], val["tree_stage"], val["tree_version"], val["tree_version_at"], val["tree_hash"]) == \
            ("patrol", "production", "v-1", "2026-11-02T14:05:00Z", "stored-hash")
        # and it really builds through the bundle's SubTree, not the working copy
        root = tree_builder.build_tree(val["tree_xml"], {}, subtrees=val["subtrees"], subtree_resolver=None)
        assert root is not None

    def test_a_changed_or_missing_hidden_copy_refuses_the_run(self, tmp_path):
        import os
        files = _files(tmp_path)
        cached = self._marked(files)
        self._write_tree(files, f".xpv.{self.KEY}.dock_seq", "<AlwaysSuccess/>")    # edited on the robot
        val = task_sync.build_run_task_val("t1", None, cached, files)
        assert val["tree_error"].startswith("This robot's copy of patrol's Production version is missing or was "
                                            "changed on the robot.")
        assert val["tree_xml"] == "" and val["resync_files"] is True
        cached = self._marked(files)
        os.remove(os.path.join(files["xparo_custom_behaviors_folder_path"], "custom_aiml",
                               f".xpv.{self.KEY}.dock_seq.xml"))
        val = task_sync.build_run_task_val("t1", None, cached, files)
        assert "missing or was changed" in val["tree_error"]   # dock_seq.xml on disk is NOT used instead

    def test_intact_copies_of_any_stored_shape_verify(self, tmp_path):
        # tree_files hashes the text the dashboard sent; the robot wrote it
        # with tree_file_content. A tree that starts with whitespace, or is
        # a one-tree <root> document, came back un-wrapped and stripped by
        # resolve_tree_xml, so its intact copy was refused on every run.
        sub = f".xpv.{self.KEY}.dock_seq"
        for main_body in (f'\n  <Sequence><SubTree ID="{sub}"/></Sequence>',
                          '<root BTCPP_format="4" main_tree_to_execute="MainTree"><BehaviorTree ID="MainTree">'
                          f'<Sequence><SubTree ID="{sub}"/></Sequence></BehaviorTree></root>'):
            here = tmp_path / str(len(main_body))
            here.mkdir()
            files = _files(here)
            cached = self._marked(files, main_body=main_body)
            val = task_sync.build_run_task_val("t1", None, cached, files)
            assert val["tree_error"] == "", main_body
            assert f'<SubTree ID="{sub}"/>' in val["tree_xml"]
            assert task_sync.hidden_file_hashes(files)[f".xpv.{self.KEY}"] == sync_hash.content_hash(main_body)
            # and a changed copy of that shape is still refused
            self._write_tree(files, f".xpv.{self.KEY}", main_body.replace("Sequence", "Fallback"))
            assert "missing or was changed" in task_sync.build_run_task_val("t1", None, cached, files)["tree_error"]

    def test_tree_error_is_passed_through(self, tmp_path):
        files = _files(tmp_path)
        cached = {"t1": {"behaviour_tree_name": "patrol", "stage": "production",
                         "tree_error": "The tree patrol no longer exists (deleted or renamed)."}}
        val = task_sync.build_run_task_val("t1", None, cached, files)
        assert val["tree_error"] == "The tree patrol no longer exists (deleted or renamed)."
        assert val["tree_xml"] == "" and val["tree_hash"] == ""

    def test_tree_hash_is_of_the_file_actually_loaded_for_a_working_copy(self, tmp_path):
        files = _files(tmp_path)
        self._write_tree(files, "patrol", "<Wait seconds=\"9\"/>")
        val = task_sync.build_run_task_val("t1", None, {"t1": {"behaviour_tree_name": "patrol"}}, files)
        assert val["tree_hash"] == sync_hash.content_hash('<Wait seconds="9"/>')
        assert task_sync.tree_file_hash("patrol", files) == val["tree_hash"]
        assert task_sync.tree_file_hash("nope", files) == ""


class TestHiddenCopiesOnTheRobot:
    KEY = "3f9a0c1b7d22"

    def test_hidden_names_are_written_but_never_part_of_the_file_sync(self, tmp_path):
        from xparo import remote_ops
        engine = _make_engine(tmp_path)
        hidden = f".xpv.{self.KEY}"
        engine.on_ws_message('ws', {"custom_aiml": {hidden: "<LoadNextDelivery />", "patrol": "<AlwaysSuccess/>"}})
        folder = tmp_path / "custom_aiml"
        assert (folder / f"{hidden}.xml").exists()
        assert remote_ops.safe_path_in(str(folder), hidden + ".xml") is not None
        state = engine.get_local_file_state()
        assert set(state["custom_aiml"]) == {"patrol"}
        assert state["hidden_files"] == {hidden: sync_hash.content_hash("<LoadNextDelivery />")}
        assert hidden not in engine.get_local_files()["custom_aiml"]

    def test_unreferenced_hidden_files_are_pruned_once_old(self, tmp_path):
        import os
        engine = _make_engine(tmp_path)
        folder = tmp_path / "custom_aiml"
        folder.mkdir()
        old_key, new_key = "aaaaaaaaaaaa", self.KEY
        for name in (f".xpv.{old_key}", f".xpv.{old_key}.dock_seq", f".xpv.{new_key}", f".xpv.{new_key}.dock_seq",
                     "patrol"):
            (folder / f"{name}.xml").write_text("<x/>")
        tasks = {"t1": {"behaviour_tree_name": f".xpv.{new_key}",
                        "tree_files": {f".xpv.{new_key}": "h", f".xpv.{new_key}.dock_seq": "h"}}}
        engine.sync_custom_tasks(tasks)       # all fresh: nothing removed yet
        assert (folder / f".xpv.{old_key}.xml").exists()
        long_ago = time.time() - 3600
        for path in folder.iterdir():
            os.utime(path, (long_ago, long_ago))
        engine.sync_custom_tasks(tasks)
        assert sorted(p.name for p in folder.iterdir()) == sorted(
            [f".xpv.{new_key}.xml", f".xpv.{new_key}.dock_seq.xml", "patrol.xml"])
