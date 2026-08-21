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

from xparo.bt_engine import task_sync
from xparo.bt_engine.executor import BehaviorTreeExecutor


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

    def test_a_name_reads_the_matching_custom_behaviors_file(self, tmp_path):
        files = _files(tmp_path)
        pth = tmp_path / "custom_behaviors" / "quick_delivery_tree.xml"
        pth.write_text('<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
                        '<BehaviorTree ID="MainTree">\n<PlayAudio file_path="/x.mp3" />\n</BehaviorTree>\n</root>')

        result = task_sync.resolve_tree_xml("quick_delivery_tree", files)

        assert result.strip() == '<PlayAudio file_path="/x.mp3" />'

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
            f.write('<BehaviorTree ID="MainTree"><LoadNextDelivery /></BehaviorTree>')
        custom_tasks = {"t1": {
            "behaviour_tree_name": "", "save_task_history": True,
            "blackboard_mapping": {"dock_name": {"mapping_type": "default", "value": "dock-1"}},
            "params": [],
        }}

        val = task_sync.build_run_task_val("t1", None, custom_tasks, files)

        assert val == {
            "task_id": "t1",
            "tree_xml": "<LoadNextDelivery />",
            "blackboard": {"dock_name": "dock-1"},
            "save_task_history": True,
            # No "stage" key on the cached task -- defaults to
            # "development", the least-trusted reading (see
            # build_run_task_val's own comment).
            "stage": "development",
        }


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
            f.write('<BehaviorTree ID="MainTree"><LoadNextDelivery /></BehaviorTree>')

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
