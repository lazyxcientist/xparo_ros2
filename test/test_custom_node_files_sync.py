"""Multi-language custom BT node system, Phase 5/6/7: syncing
apps/analytics/models.py's CustomFile/CustomNodeDefinition source (any
language) to the robot and registering it. Python goes through the exact
same plugin_loader.register_plugins() Phase 12/13 already established
(mirroring test_inline_bt_nodes.py's own structure closely); JavaScript/
C++/Bash register directly into NODE_REGISTRY via runners.py -- see that
module's own docstring for why. These tests exercise real subprocesses/a
real compile, matching this repo's own established "test against the real
thing, don't mock" convention (test_remote_ops.py) -- loader correctness
itself is covered by test_plugin_loader.py, runner correctness by
test_runners.py.
"""
from xparo.bt_engine.node_registry import NODE_REGISTRY

NAVIGATE_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class Navigate(CustomBTNode):
    XML_TAG = "Navigate"

    def update(self):
        return common.Status.SUCCESS
'''

DOCK_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class Dock(CustomBTNode):
    XML_TAG = "Dock"

    def update(self):
        return common.Status.SUCCESS
'''

JS_NAVIGATE_SOURCE = '''
class Navigate extends XparoNode {
  tick() {
    return this.SUCCESS;
  }
}
module.exports = Navigate;
'''

BASH_NAVIGATE_SOURCE = '''#!/usr/bin/env bash
exit 0
'''

CPP_NAVIGATE_SOURCE = '''
#include <behaviortree_cpp/behavior_tree.h>

class Navigate : public BT::SyncActionNode
{
public:
    Navigate(const std::string& name, const BT::NodeConfig& config)
        : BT::SyncActionNode(name, config) {}

    static BT::PortsList providedPorts() { return {}; }

    BT::NodeStatus tick() override
    {
        return BT::NodeStatus::SUCCESS;
    }
};
'''


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-custom-node-file-sync-test", **kwargs)
    # Same reasoning as test_plugin_loader.py/test_inline_bt_nodes.py's own
    # _make_engine -- redirect off the real repo checkout, which
    # sync_custom_node_files would otherwise really write files into.
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    return engine


class TestEngineSyncCustomNodeFilesPython:
    def test_resync_persists_one_file_per_exposed_python_file_and_registers_it(self, tmp_path):
        engine = _make_engine(tmp_path)

        try:
            engine.sync_custom_node_files({"navigate": {"language": "python", "source": NAVIGATE_SOURCE}})

            written = (tmp_path / "custom_node_files" / "navigate.py").read_text()
            assert written == NAVIGATE_SOURCE
            assert "Navigate" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_an_unrecognized_language_is_skipped_not_written_or_registered(self, tmp_path):
        engine = _make_engine(tmp_path)

        engine.sync_custom_node_files({
            "navigate": {"language": "rust", "source": "fn main() {}", "xml_tag": "Navigate"},
        })

        assert not (tmp_path / "custom_node_files" / "navigate.py").exists()
        assert not (tmp_path / "custom_node_files" / "navigate.rs").exists()
        assert "Navigate" not in NODE_REGISTRY

    def test_a_cpp_file_that_fails_to_compile_is_skipped_not_registered_rest_of_sync_still_proceeds(self, tmp_path):
        """Mirrors plugin_loader.load_plugins' own "one bad file
        contributes nothing, never blocks the rest" posture -- a
        malformed C++ entry alongside a good Python one must not stop the
        Python one from registering."""
        engine = _make_engine(tmp_path)
        NODE_REGISTRY.pop("Navigate", None)
        NODE_REGISTRY.pop("Dock", None)

        try:
            engine.sync_custom_node_files({
                "navigate": {"language": "cpp", "source": "not even valid c++", "xml_tag": "Navigate"},
                "dock": {"language": "python", "source": DOCK_SOURCE},
            })

            assert "Navigate" not in NODE_REGISTRY
            assert "Dock" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("Navigate", None)
            NODE_REGISTRY.pop("Dock", None)

    def test_a_file_removed_from_the_dict_has_its_file_deleted_and_is_unregistered(self, tmp_path):
        engine = _make_engine(tmp_path)
        NODE_REGISTRY.pop("Navigate", None)
        NODE_REGISTRY.pop("Dock", None)

        try:
            engine.sync_custom_node_files({
                "navigate": {"language": "python", "source": NAVIGATE_SOURCE},
                "dock": {"language": "python", "source": DOCK_SOURCE},
            })
            assert (tmp_path / "custom_node_files" / "navigate.py").exists()

            engine.sync_custom_node_files({"dock": {"language": "python", "source": DOCK_SOURCE}})

            assert not (tmp_path / "custom_node_files" / "navigate.py").exists()
            assert "Navigate" not in NODE_REGISTRY
            assert "Dock" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("Navigate", None)
            NODE_REGISTRY.pop("Dock", None)

    def test_un_exposing_a_node_removed_from_the_sync_payload_unregisters_it(self, tmp_path):
        """SAVE->DELETE_custom_node_definition on the Django side means
        this file simply stops appearing in _get_custom_node_files_for_sync's
        output -- confirms that alone is enough to retire the tag here,
        without any special "un-expose" signal in the payload itself."""
        engine = _make_engine(tmp_path)
        NODE_REGISTRY.pop("Navigate", None)

        try:
            engine.sync_custom_node_files({"navigate": {"language": "python", "source": NAVIGATE_SOURCE}})
            assert "Navigate" in NODE_REGISTRY

            engine.sync_custom_node_files({})

            assert "Navigate" not in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_an_updated_source_is_rewritten_and_reregistered(self, tmp_path):
        engine = _make_engine(tmp_path)

        try:
            engine.sync_custom_node_files({"navigate": {"language": "python", "source": NAVIGATE_SOURCE}})
            first_builder = NODE_REGISTRY["Navigate"]

            updated_source = NAVIGATE_SOURCE.replace("SUCCESS", "FAILURE")
            engine.sync_custom_node_files({"navigate": {"language": "python", "source": updated_source}})

            assert (tmp_path / "custom_node_files" / "navigate.py").read_text() == updated_source
            assert NODE_REGISTRY["Navigate"] is not first_builder
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_startup_with_no_persisted_folder_is_a_safe_noop(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.sync_custom_node_files()  # no custom_node_files/ folder yet -- must not raise

    def test_startup_loads_whatever_was_persisted_by_an_earlier_resync(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.sync_custom_node_files({"navigate": {"language": "python", "source": NAVIGATE_SOURCE}})
        NODE_REGISTRY.pop("Navigate", None)  # simulate a fresh process that hasn't loaded it yet

        try:
            fresh_engine = _make_engine(tmp_path)
            fresh_engine.sync_custom_node_files()  # startup case -- reads the files the resync above wrote

            assert "Navigate" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_inline_nodes_and_custom_node_files_syncs_dont_stomp_each_others_tags(self, tmp_path):
        """The two mechanisms track their own _inline_node_tags /
        _custom_node_file_tags sets separately -- a resync of one must
        never unregister a tag the OTHER most recently registered."""
        engine = _make_engine(tmp_path)

        try:
            engine.sync_bt_inline_nodes({"Dock": DOCK_SOURCE})
            engine.sync_custom_node_files({"navigate": {"language": "python", "source": NAVIGATE_SOURCE}})

            assert "Dock" in NODE_REGISTRY
            assert "Navigate" in NODE_REGISTRY

            engine.sync_custom_node_files({"navigate": {"language": "python", "source": NAVIGATE_SOURCE}})

            assert "Dock" in NODE_REGISTRY  # untouched by the custom_node_files resync
        finally:
            NODE_REGISTRY.pop("Dock", None)
            NODE_REGISTRY.pop("Navigate", None)


class TestEngineSyncCustomNodeFilesJavaScript:
    def test_resync_writes_the_js_file_and_runtime_and_registers_the_real_xml_tag(self, tmp_path):
        engine = _make_engine(tmp_path)

        try:
            engine.sync_custom_node_files({
                "navigate": {"language": "javascript", "source": JS_NAVIGATE_SOURCE, "xml_tag": "Navigate", "node_type": "action", "ports": []},
            })

            assert (tmp_path / "custom_node_files" / "navigate.js").read_text() == JS_NAVIGATE_SOURCE
            assert (tmp_path / "custom_node_files" / "js_runtime" / "js_host.js").exists()
            assert (tmp_path / "custom_node_files" / "js_runtime" / "xparo_node.js").exists()
            assert "Navigate" in NODE_REGISTRY

            from xparo.bt_engine import tree_builder
            import py_trees
            root = tree_builder.build_tree("<Navigate />", {})
            tree = py_trees.trees.BehaviourTree(root)
            tree.tick()
            assert root.status == py_trees.common.Status.SUCCESS
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_startup_reregisters_a_js_tag_from_the_persisted_manifest(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.sync_custom_node_files({
            "navigate": {"language": "javascript", "source": JS_NAVIGATE_SOURCE, "xml_tag": "Navigate", "node_type": "action", "ports": []},
        })
        NODE_REGISTRY.pop("Navigate", None)

        try:
            fresh_engine = _make_engine(tmp_path)
            fresh_engine.sync_custom_node_files()

            assert "Navigate" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("Navigate", None)


class TestEngineSyncCustomNodeFilesBash:
    def test_resync_writes_an_executable_sh_file_and_registers_the_real_xml_tag(self, tmp_path):
        engine = _make_engine(tmp_path)
        import os

        try:
            engine.sync_custom_node_files({
                "navigate": {"language": "bash", "source": BASH_NAVIGATE_SOURCE, "xml_tag": "Navigate", "node_type": "action", "ports": []},
            })

            script_path = tmp_path / "custom_node_files" / "navigate.sh"
            assert script_path.read_text() == BASH_NAVIGATE_SOURCE
            assert os.access(script_path, os.X_OK)
            assert "Navigate" in NODE_REGISTRY

            from xparo.bt_engine import tree_builder
            import py_trees
            root = tree_builder.build_tree("<Navigate />", {})
            tree = py_trees.trees.BehaviourTree(root)
            tree.tick()
            assert root.status == py_trees.common.Status.SUCCESS
        finally:
            NODE_REGISTRY.pop("Navigate", None)


class TestEngineSyncCustomNodeFilesCpp:
    def test_resync_compiles_the_source_and_registers_the_real_xml_tag(self, tmp_path):
        engine = _make_engine(tmp_path)

        try:
            engine.sync_custom_node_files({
                "navigate": {"language": "cpp", "source": CPP_NAVIGATE_SOURCE, "xml_tag": "Navigate", "node_type": "action", "ports": []},
            })

            assert (tmp_path / "custom_node_files" / "navigate.cpp").exists()
            assert (tmp_path / "custom_node_files" / "cpp_build" / "navigate").exists()
            assert "Navigate" in NODE_REGISTRY

            from xparo.bt_engine import tree_builder
            import py_trees
            root = tree_builder.build_tree("<Navigate />", {})
            tree = py_trees.trees.BehaviourTree(root)
            tree.tick()
            assert root.status == py_trees.common.Status.SUCCESS
        finally:
            NODE_REGISTRY.pop("Navigate", None)


class TestOnWsMessageCustomNodeFileSync:
    def test_custom_node_files_key_dispatches_to_sync_custom_node_files(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.transport.send = lambda message, command_for=None: None

        try:
            engine.on_ws_message('ws', {"custom_node_files": {"navigate": {"language": "python", "source": NAVIGATE_SOURCE}}})
            assert "Navigate" in NODE_REGISTRY
            assert (tmp_path / "custom_node_files" / "navigate.py").exists()
        finally:
            NODE_REGISTRY.pop("Navigate", None)
