"""R4H (tasks-robots) robot-side fixes.

NODESYNC-35: a <SubTree ID> pointing at a tree that exists but is empty was
reported as "doesn't exist on this robot (check the name, and that the robot
has synced ...)", which sent people checking names and sync instead of
filling the tree. A run now says the tree is empty; a tree the robot really
doesn't have still says it doesn't exist.

KUNAL-28: a run whose override_params named no param of the task said so in
its result, but the Task history row it sends had no list of those names, so
the dashboard's run report couldn't repeat it.
"""
import time
from unittest.mock import MagicMock

from xparo.bt_engine import task_sync, tree_builder
from xparo.bt_engine.executor import BehaviorTreeExecutor
from xparo.bt_engine.run_task import handle_run_task
from xparo.bt_engine.xml_parser import tree_file_content

EMPTY_DOCUMENT = '<root BTCPP_format="4"><BehaviorTree ID="room_is_free"></BehaviorTree></root>'
MAIN = '<Sequence><LoadNextDelivery /><SubTree ID="room_is_free"/></Sequence>'


def _files(tmp_path):
    behaviors = tmp_path / "custom_behaviors"
    (behaviors / "custom_aiml").mkdir(parents=True)
    (tmp_path / "custom_envs").mkdir()
    return {
        "behavior": str(tmp_path / "default.xml"),
        "xparo_custom_behaviors_folder_path": str(behaviors),
        "xparo_custom_evns_folder_path": str(tmp_path / "custom_envs"),
    }


def _errors(problems):
    return [p["message"] for p in problems if p["level"] == "error"]


class TestEmptySubTree:
    def test_an_empty_subtree_sent_by_the_dashboard_says_it_is_empty(self):
        for empty in ("", "   \n", EMPTY_DOCUMENT, "<!-- nothing yet -->",
                      '<root><TreeNodesModel><Action ID="X"/></TreeNodesModel></root>'):
            errors = _errors(tree_builder.validate_tree(MAIN, subtrees={"room_is_free": empty}))
            assert len(errors) == 1, (empty, errors)
            assert "'room_is_free', which is empty" in errors[0], (empty, errors)
            assert "doesn't exist" not in errors[0]

    def test_a_subtree_the_robot_does_not_have_still_says_it_does_not_exist(self):
        errors = _errors(tree_builder.validate_tree(MAIN, subtree_resolver=lambda name: None))
        assert len(errors) == 1 and "which doesn't exist on this robot" in errors[0]
        errors = _errors(tree_builder.validate_tree(MAIN))
        assert len(errors) == 1 and "which doesn't exist on this robot" in errors[0]

    def test_an_empty_synced_copy_on_the_robot_says_it_is_empty(self, tmp_path):
        files = _files(tmp_path)
        (tmp_path / "custom_behaviors" / "custom_aiml" / "room_is_free.xml").write_text(tree_file_content(""))
        assert task_sync.resolve_subtree_xml("room_is_free", files) == ""
        errors = _errors(tree_builder.validate_tree(
            MAIN, subtree_resolver=lambda name: task_sync.resolve_subtree_xml(name, files)))
        assert len(errors) == 1 and "'room_is_free', which is empty" in errors[0]

    def test_resolve_subtree_xml_tells_missing_from_empty(self, tmp_path):
        files = _files(tmp_path)
        assert task_sync.resolve_subtree_xml("never_synced", files) is None
        assert task_sync.resolve_subtree_xml("", files) is None
        (tmp_path / "custom_behaviors" / "custom_aiml" / "dock.xml").write_text(tree_file_content("<LoadNextDelivery />"))
        assert task_sync.resolve_subtree_xml("dock", files).strip() == "<LoadNextDelivery />"
        errors = _errors(tree_builder.validate_tree(
            '<SubTree ID="dock"/>', subtree_resolver=lambda name: task_sync.resolve_subtree_xml(name, files)))
        assert errors == []

    def test_a_filled_subtree_still_builds_and_an_unreadable_one_says_so(self):
        root = tree_builder.build_tree(MAIN, {}, subtrees={"room_is_free": "<LoadNextDelivery />"})
        assert root is not None
        errors = _errors(tree_builder.validate_tree(MAIN, subtrees={"room_is_free": "<Sequence><Broken"}))
        assert len(errors) == 1 and "can't be read" in errors[0]

    def test_the_run_result_names_the_empty_tree(self):
        responses = []
        handle_run_task(BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock()),
                        {"task_id": "t1", "tree_xml": MAIN, "subtrees": {"room_is_free": ""}, "blackboard": {},
                         "stage": "development"},
                        responses.append, xparo_stage="development")
        result = responses[0]["TASK_RESULT"]
        assert result["success"] is False and result["outcome"] == "invalid_tree"
        text = f"{result['error']} {result['explanation']}"
        assert "'room_is_free', which is empty" in text
        assert "doesn't exist" not in text

    def test_the_engine_resolves_subtrees_with_resolve_subtree_xml(self, tmp_path):
        """engine.py hands handle_run_task a resolver that returns None for a
        missing tree (resolve_tree_xml returns '' for both)."""
        import inspect
        from xparo import engine
        source = inspect.getsource(engine.Engine)
        assert "task_sync.resolve_subtree_xml(name, self.files)" in source
        assert "task_sync.resolve_tree_xml(name, self.files)" not in source


class TestIgnoredOverridesInHistory:
    def test_the_history_row_lists_the_ignored_override_names(self):
        responses, history = [], []
        handle_run_task(BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock()),
                        {"task_id": "t1", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "stage": "development",
                         "save_task_history": True, "ignored_overrides": ["destination"]},
                        responses.append, add_task_history=history.append, xparo_stage="development")
        assert responses[0]["TASK_RESULT"]["ignored_override_params"] == ["destination"]
        assert history[0]["input_data"]["ignored_override_params"] == ["destination"]
        assert "override_params 'destination' was ignored" in history[0]["output_data"]["report"]["explanation"]

    def test_no_ignored_overrides_adds_nothing(self):
        history = []
        handle_run_task(BehaviorTreeExecutor(node=MagicMock(), engine=MagicMock()),
                        {"task_id": "t1", "tree_xml": "<LoadNextDelivery />", "blackboard": {}, "stage": "development",
                         "save_task_history": True},
                        lambda message: None, add_task_history=history.append, xparo_stage="development")
        assert "ignored_override_params" not in history[0]["input_data"]
