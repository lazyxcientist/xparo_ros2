"""Behaviour Tree redesign Phase 12 (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): the modular external plugin loader.
"""
import json

from py_trees import common

from xparo.bt_engine import tree_builder
from xparo.bt_engine.node_registry import NODE_REGISTRY
from xparo.bt_engine.plugin_loader import CustomBTNode, load_plugins, register_plugins

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
