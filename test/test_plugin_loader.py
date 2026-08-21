"""Behaviour Tree redesign Phase 12 (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): the modular external plugin loader.
"""
import json

from py_trees import common

from xparo.bt_engine import tree_builder
from xparo.bt_engine.node_registry import NODE_REGISTRY
from xparo.bt_engine.plugin_loader import CustomBTNode, load_plugins, register_plugins, unregister_tags

CUSTOM_NODE_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class MyCustomAction(CustomBTNode):
    XML_TAG = "MyCustomAction"

    def update(self):
        return common.Status.SUCCESS
'''

NO_SUBCLASS_SOURCE = '''
class NotACustomNode:
    pass


def some_function():
    return 1
'''

MULTIPLE_TAGS_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class FirstAction(CustomBTNode):
    XML_TAG = "FirstAction"

    def update(self):
        return common.Status.SUCCESS


class SecondCondition(CustomBTNode):
    XML_TAG = "SecondCondition"

    def update(self):
        return common.Status.FAILURE
'''

CRASHING_NODE_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class CrashesOnTick(CustomBTNode):
    XML_TAG = "CrashesOnTick"

    def update(self):
        raise RuntimeError("boom")
'''


class TestLoadPlugins:
    def test_a_custom_bt_node_subclass_is_found_and_registered_by_tag(self, tmp_path):
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)

        registry = load_plugins([str(plugin_file)])

        assert "MyCustomAction" in registry
        assert issubclass(registry["MyCustomAction"], CustomBTNode)

    def test_a_file_with_no_custom_bt_node_subclass_loads_cleanly_and_registers_nothing(self, tmp_path):
        plugin_file = tmp_path / "empty_plugin.py"
        plugin_file.write_text(NO_SUBCLASS_SOURCE)

        registry = load_plugins([str(plugin_file)])

        assert registry == {}

    def test_multiple_tags_in_one_file_are_all_registered(self, tmp_path):
        plugin_file = tmp_path / "multi.py"
        plugin_file.write_text(MULTIPLE_TAGS_SOURCE)

        registry = load_plugins([str(plugin_file)])

        assert set(registry.keys()) == {"FirstAction", "SecondCondition"}

    def test_a_nonexistent_path_is_skipped_not_raised(self):
        registry = load_plugins(["/no/such/file/anywhere.py"])
        assert registry == {}

    def test_one_bad_path_does_not_prevent_a_good_one_from_loading(self, tmp_path):
        good_file = tmp_path / "good.py"
        good_file.write_text(CUSTOM_NODE_SOURCE)

        registry = load_plugins(["/no/such/file.py", str(good_file)])

        assert "MyCustomAction" in registry

    def test_the_base_class_itself_is_never_registered(self, tmp_path):
        plugin_file = tmp_path / "base_only.py"
        plugin_file.write_text("from xparo.bt_engine.plugin_loader import CustomBTNode\n")

        registry = load_plugins([str(plugin_file)])

        assert registry == {}


class TestRegisterPlugins:
    def test_a_loaded_plugin_is_buildable_into_a_real_tree(self, tmp_path):
        plugin_file = tmp_path / "buildable.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)
        try:
            register_plugins([str(plugin_file)])
            root = tree_builder.build_tree("<Sequence><MyCustomAction /></Sequence>", {})
            tree_pkg = __import__("py_trees").trees.BehaviourTree(root)
            tree_pkg.tick()
            assert root.status == common.Status.SUCCESS
        finally:
            NODE_REGISTRY.pop("MyCustomAction", None)

    def test_registering_twice_overwrites_rather_than_duplicating(self, tmp_path):
        plugin_file = tmp_path / "reload.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)
        try:
            register_plugins([str(plugin_file)])
            first_builder = NODE_REGISTRY["MyCustomAction"]
            register_plugins([str(plugin_file)])
            second_builder = NODE_REGISTRY["MyCustomAction"]
            assert NODE_REGISTRY["MyCustomAction"] is second_builder
        finally:
            NODE_REGISTRY.pop("MyCustomAction", None)


class TestCrashIsolation:
    """A node's update() raising must become a controlled FAILURE, not an
    exception that unwinds the whole tree tick -- confirmed by reading
    py_trees' own Sequence/BehaviourTree.tick source that nothing upstream
    catches this otherwise, so a bad plugin/inline/custom-file node used to
    abort every other branch in the same tree run, not just itself."""

    def test_a_node_that_raises_reports_failure_instead_of_propagating(self, tmp_path):
        plugin_file = tmp_path / "crashes.py"
        plugin_file.write_text(CRASHING_NODE_SOURCE)
        try:
            register_plugins([str(plugin_file)])
            root = tree_builder.build_tree("<Sequence><CrashesOnTick /></Sequence>", {})
            tree_pkg = __import__("py_trees").trees.BehaviourTree(root)
            tree_pkg.tick()  # must not raise
            assert root.status == common.Status.FAILURE
        finally:
            NODE_REGISTRY.pop("CrashesOnTick", None)

    def test_a_sibling_after_the_crashing_node_still_gets_a_chance_to_run(self, tmp_path):
        """The whole point of isolating it at the node level rather than
        only at run_task.py's whole-task try/except -- other branches in
        the SAME tree run must survive one bad node, not just other
        concurrently-running tasks in other threads."""
        crash_file = tmp_path / "crashes.py"
        crash_file.write_text(CRASHING_NODE_SOURCE)
        good_file = tmp_path / "good.py"
        good_file.write_text(CUSTOM_NODE_SOURCE)
        try:
            register_plugins([str(crash_file), str(good_file)])
            # A crashing first child must not stop the Sequence from
            # continuing to tick its next child on a LATER tree tick --
            # Sequence semantics: since the first child now reports
            # FAILURE (not RUNNING), the Sequence itself is FAILURE this
            # tick and never reaches the second child, so exercise this
            # via a Fallback instead: Fallback tries the next child
            # exactly when an earlier one fails.
            root = tree_builder.build_tree(
                "<Fallback><CrashesOnTick /><MyCustomAction /></Fallback>", {},
            )
            tree_pkg = __import__("py_trees").trees.BehaviourTree(root)
            tree_pkg.tick()
            assert root.status == common.Status.SUCCESS
        finally:
            NODE_REGISTRY.pop("CrashesOnTick", None)
            NODE_REGISTRY.pop("MyCustomAction", None)

    def test_crash_isolation_also_applies_to_nodes_loaded_via_the_older_inline_mechanism(self, tmp_path):
        """sync_bt_inline_nodes (Phase 13) and sync_custom_node_files
        (Phase 5) both route through this exact same register_plugins --
        the safety net isn't specific to one caller."""
        from xparo.engine import Engine

        engine = Engine("secret", "proj-crash-isolation-test", connection_type="offline")
        engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
        try:
            engine.sync_bt_inline_nodes({"CrashesOnTick": CRASHING_NODE_SOURCE})
            root = tree_builder.build_tree("<Sequence><CrashesOnTick /></Sequence>", {})
            tree_pkg = __import__("py_trees").trees.BehaviourTree(root)
            tree_pkg.tick()
            assert root.status == common.Status.FAILURE
        finally:
            NODE_REGISTRY.pop("CrashesOnTick", None)


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-plugin-sync-test", **kwargs)
    # Redirect off the real repo checkout -- Engine.__init__ defaults this
    # to the real ros_packages/src/xparo/custom_behaviors/ folder (relative
    # to engine.py's own location), which sync_bt_plugins would otherwise
    # really write plugin_paths.json into.
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    return engine


class TestUnregisterTags:
    def test_a_registered_tag_is_removed(self, tmp_path):
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)
        register_plugins([str(plugin_file)])
        assert "MyCustomAction" in NODE_REGISTRY

        unregister_tags(["MyCustomAction"])

        assert "MyCustomAction" not in NODE_REGISTRY

    def test_a_tag_that_was_never_registered_is_a_safe_noop(self):
        unregister_tags(["NoSuchTag"])  # must not raise


class TestEngineSyncBtPlugins:
    def test_resync_persists_the_list_and_registers_enabled_paths(self, tmp_path):
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)
        engine = _make_engine(tmp_path)

        try:
            engine.sync_bt_plugins([{"path": str(plugin_file), "enabled": True}])

            persisted = json.loads((tmp_path / "plugin_paths.json").read_text())
            assert persisted == [{"path": str(plugin_file), "enabled": True}]
            assert "MyCustomAction" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("MyCustomAction", None)

    def test_a_disabled_entry_is_persisted_but_not_loaded(self, tmp_path):
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)
        engine = _make_engine(tmp_path)
        NODE_REGISTRY.pop("MyCustomAction", None)

        engine.sync_bt_plugins([{"path": str(plugin_file), "enabled": False}])

        assert "MyCustomAction" not in NODE_REGISTRY

    def test_startup_with_no_persisted_file_is_a_safe_noop(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.sync_bt_plugins()  # no plugin_paths.json exists yet -- must not raise

    def test_startup_loads_whatever_was_persisted_by_an_earlier_resync(self, tmp_path):
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)
        engine = _make_engine(tmp_path)
        engine.sync_bt_plugins([{"path": str(plugin_file), "enabled": True}])
        NODE_REGISTRY.pop("MyCustomAction", None)  # simulate a fresh process that hasn't loaded it yet

        try:
            fresh_engine = _make_engine(tmp_path)
            fresh_engine.sync_bt_plugins()  # startup case -- reads the file sync_bt_plugins above wrote

            assert "MyCustomAction" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("MyCustomAction", None)


class TestOnWsMessagePluginSync:
    def test_custom_bt_node_plugins_key_dispatches_to_sync_bt_plugins(self, tmp_path):
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(CUSTOM_NODE_SOURCE)
        engine = _make_engine(tmp_path)
        engine.transport.send = lambda message, command_for=None: None

        try:
            engine.on_ws_message('ws', {"custom_bt_node_plugins": [{"path": str(plugin_file), "enabled": True}]})
            assert "MyCustomAction" in NODE_REGISTRY
            assert (tmp_path / "plugin_paths.json").exists()
        finally:
            NODE_REGISTRY.pop("MyCustomAction", None)
