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
import json

import pytest

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

            written = (tmp_path / "custom_node_files" / "python" / "navigate.py").read_text()
            assert written == NAVIGATE_SOURCE
            assert "Navigate" in NODE_REGISTRY

            # Bidirectional file sync: a fresh Django sync is the new
            # authoritative baseline -- content_hash/synced_at land in the
            # manifest immediately, not just on some later reconciliation.
            from xparo.sync_hash import content_hash
            manifest = json.loads((tmp_path / "custom_node_files" / "manifest.json").read_text())
            assert manifest["navigate"]["content_hash"] == content_hash(NAVIGATE_SOURCE, "")
            assert manifest["navigate"]["synced_at"]
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_an_unrecognized_language_is_skipped_not_written_or_registered(self, tmp_path):
        engine = _make_engine(tmp_path)

        engine.sync_custom_node_files({
            "navigate": {"language": "rust", "source": "fn main() {}", "xml_tag": "Navigate"},
        })

        assert not (tmp_path / "custom_node_files" / "python" / "navigate.py").exists()
        assert not (tmp_path / "custom_node_files" / "rust" / "navigate.rs").exists()
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
            failures = engine.sync_custom_node_files({
                "navigate": {"language": "cpp", "source": "not even valid c++", "xml_tag": "Navigate"},
                "dock": {"language": "python", "source": DOCK_SOURCE},
            })

            assert "Navigate" not in NODE_REGISTRY
            assert "Dock" in NODE_REGISTRY
            # Real fix: this used to only ever be printed to the robot's
            # own stdout, invisible to whoever edited the file from the
            # dashboard -- now it's returned so on_ws_message can relay it.
            assert len(failures) == 1
            assert failures[0]["name"] == "navigate"
            assert failures[0]["language"] == "cpp"
            assert failures[0]["reason"]
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
            assert (tmp_path / "custom_node_files" / "python" / "navigate.py").exists()

            engine.sync_custom_node_files({"dock": {"language": "python", "source": DOCK_SOURCE}})

            assert not (tmp_path / "custom_node_files" / "python" / "navigate.py").exists()
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

            assert (tmp_path / "custom_node_files" / "python" / "navigate.py").read_text() == updated_source
            assert NODE_REGISTRY["Navigate"] is not first_builder
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_a_pre_existing_manifest_entry_with_no_hash_is_bootstrapped_not_flagged(self, tmp_path):
        """Bidirectional file sync, Part 5: a manifest.json written before
        hash-tracking existed has no content_hash key at all -- reading it
        must compute one from whatever's on disk right now and treat that
        as the new baseline, not raise or silently leave it un-hashed
        forever."""
        from xparo.sync_hash import content_hash

        engine = _make_engine(tmp_path)
        node_files_dir = tmp_path / "custom_node_files"
        (node_files_dir / "python").mkdir(parents=True)
        (node_files_dir / "python" / "navigate.py").write_text(NAVIGATE_SOURCE)
        (node_files_dir / "manifest.json").write_text(json.dumps({
            "navigate": {"language": "python", "xml_tag": "", "node_type": "action", "ports": [], "header_source": ""},
        }))

        try:
            engine.sync_custom_node_files()  # startup case, no Django payload

            assert "Navigate" in NODE_REGISTRY
            manifest = json.loads((node_files_dir / "manifest.json").read_text())
            assert manifest["navigate"]["content_hash"] == content_hash(NAVIGATE_SOURCE, "")
            assert manifest["navigate"]["synced_at"]
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

            assert (tmp_path / "custom_node_files" / "javascript" / "navigate.js").read_text() == JS_NAVIGATE_SOURCE
            assert (tmp_path / "custom_node_files" / "javascript" / "js_runtime" / "js_host.js").exists()
            assert (tmp_path / "custom_node_files" / "javascript" / "js_runtime" / "xparo_node.js").exists()
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

            script_path = tmp_path / "custom_node_files" / "bash" / "navigate.sh"
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


class TestEngineSyncCustomNodeFilesEmpty:
    """R2 node-sync test: a JavaScript node exposed and saved before any
    code was written reached the robot as a 0-byte file and its tag was
    still registered -- no warning anywhere, it could only fail in a run."""

    def test_an_empty_js_or_bash_file_is_a_sync_failure_not_a_registered_tag(self, tmp_path):
        engine = _make_engine(tmp_path)
        NODE_REGISTRY.pop("EmptyJs", None)
        NODE_REGISTRY.pop("EmptySh", None)

        try:
            failures = engine.sync_custom_node_files({
                "empty_js": {"language": "javascript", "source": "", "xml_tag": "EmptyJs", "node_type": "action", "ports": []},
                "empty_sh": {"language": "bash", "source": "  \n", "xml_tag": "EmptySh", "node_type": "action", "ports": []},
                "navigate": {"language": "bash", "source": BASH_NAVIGATE_SOURCE, "xml_tag": "Navigate", "node_type": "action", "ports": []},
            })

            assert "EmptyJs" not in NODE_REGISTRY
            assert "EmptySh" not in NODE_REGISTRY
            assert "Navigate" in NODE_REGISTRY
            assert sorted((f["name"], f["language"]) for f in failures) == [("empty_js", "javascript"), ("empty_sh", "bash")]
            assert all("no code" in f["reason"] for f in failures)
        finally:
            for tag in ("EmptyJs", "EmptySh", "Navigate"):
                NODE_REGISTRY.pop(tag, None)


class TestEngineSyncCustomNodeFilesCpp:
    def test_resync_compiles_the_source_and_registers_the_real_xml_tag(self, tmp_path):
        engine = _make_engine(tmp_path)

        try:
            failures = engine.sync_custom_node_files({
                "navigate": {"language": "cpp", "source": CPP_NAVIGATE_SOURCE, "xml_tag": "Navigate", "node_type": "action", "ports": []},
            })

            assert (tmp_path / "custom_node_files" / "cpp" / "navigate.cpp").exists()
            assert (tmp_path / "custom_node_files" / "cpp" / "cpp_build" / "navigate").exists()
            assert "Navigate" in NODE_REGISTRY
            assert failures == []

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
            assert (tmp_path / "custom_node_files" / "python" / "navigate.py").exists()
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_custom_node_files_key_always_acks_with_registered_tags_and_no_failures(self, tmp_path):
        engine = _make_engine(tmp_path)
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(json.loads(message))

        try:
            engine.on_ws_message('ws', {"custom_node_files": {"navigate": {"language": "python", "source": NAVIGATE_SOURCE}}})
            acks = [m["CUSTOM_NODE_SYNC_RESULT"] for m in sent if "CUSTOM_NODE_SYNC_RESULT" in m]
            assert len(acks) == 1
            assert acks[0]["registered_tags"] == ["Navigate"]
            assert acks[0]["failures"] == []
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_custom_node_files_key_acks_with_a_failure_entry_for_a_bad_compile(self, tmp_path):
        engine = _make_engine(tmp_path)
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(json.loads(message))

        engine.on_ws_message('ws', {"custom_node_files": {
            "navigate": {"language": "cpp", "source": "not even valid c++", "xml_tag": "Navigate"},
        }})
        acks = [m["CUSTOM_NODE_SYNC_RESULT"] for m in sent if "CUSTOM_NODE_SYNC_RESULT" in m]
        assert len(acks) == 1
        assert acks[0]["registered_tags"] == []
        assert len(acks[0]["failures"]) == 1
        assert acks[0]["failures"][0]["name"] == "navigate"


ROOM_FREE_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class RoomFree(CustomBTNode):
    XML_TAG = "RoomFree"

    def update(self):
        return common.Status.SUCCESS
'''


class TestRobotSideEditsAreNotLostSilently:
    """NODESYNC-16: a hotfix made on the robot went back to the project's
    copy as soon as anyone saved another node (or the robot reconnected),
    with no backup and nothing in the log."""

    def _synced(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.sync_custom_node_files({"room_free_check": {"language": "python", "source": ROOM_FREE_SOURCE}})
        path = tmp_path / "custom_node_files" / "python" / "room_free_check.py"
        path.write_text(ROOM_FREE_SOURCE + "# robot-side hotfix\n")
        return engine, path

    def test_a_re_sent_unchanged_project_copy_keeps_the_robots_edit(self, tmp_path):
        engine, path = self._synced(tmp_path)
        try:
            engine.sync_custom_node_files({"room_free_check": {"language": "python", "source": ROOM_FREE_SOURCE}})

            assert path.read_text().endswith("# robot-side hotfix\n")
        finally:
            NODE_REGISTRY.pop("RoomFree", None)

    def test_the_kept_edit_is_reported_to_the_project_by_its_disk_hash(self, tmp_path):
        from xparo.sync_hash import content_hash
        engine, path = self._synced(tmp_path)
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(json.loads(message))
        try:
            engine.on_ws_message('ws', {"custom_node_files": {
                "room_free_check": {"language": "python", "source": ROOM_FREE_SOURCE}}})

            states = [m["LOCAL_FILE_STATE"] for m in sent if "LOCAL_FILE_STATE" in m]
            assert states, "the project must hear about the kept edit now, not at the next reconnect"
            assert states[0]["custom_node_files"]["room_free_check"]["content_hash"] == \
                content_hash(ROOM_FREE_SOURCE + "# robot-side hotfix\n", "")
            # NODESYNC-27: and the copy it was edited from, so the project can
            # tell a robot-only edit from a change on both sides.
            assert states[0]["custom_node_files"]["room_free_check"]["synced_hash"] == content_hash(ROOM_FREE_SOURCE, "")
        finally:
            NODE_REGISTRY.pop("RoomFree", None)

    def test_a_changed_project_copy_wins_and_the_robots_edit_is_backed_up_and_reported(self, tmp_path):
        engine, path = self._synced(tmp_path)
        new_source = ROOM_FREE_SOURCE + "# project v2\n"
        try:
            failures = engine.sync_custom_node_files({"room_free_check": {"language": "python", "source": new_source}})

            assert path.read_text() == new_source
            backups = list(path.parent.glob("room_free_check.py.robot-*"))
            assert len(backups) == 1 and backups[0].read_text().endswith("# robot-side hotfix\n")
            assert any(f["name"] == "room_free_check" and backups[0].name in f["reason"] for f in failures)
            # NODESYNC-33: and what the edit was, for the dashboard's diff
            assert [f["robot_copy"] for f in failures if f["name"] == "room_free_check"] == [backups[0].read_text()]
        finally:
            NODE_REGISTRY.pop("RoomFree", None)

    def test_a_file_nobody_edited_on_the_robot_is_not_rewritten(self, tmp_path):
        import os
        engine = _make_engine(tmp_path)
        try:
            engine.sync_custom_node_files({"room_free_check": {"language": "python", "source": ROOM_FREE_SOURCE}})
            path = tmp_path / "custom_node_files" / "python" / "room_free_check.py"
            os.utime(path, (1_000_000, 1_000_000))

            engine.sync_custom_node_files({"room_free_check": {"language": "python", "source": ROOM_FREE_SOURCE}})

            assert path.stat().st_mtime == 1_000_000  # NODESYNC-22: no rewrite of unchanged files
        finally:
            NODE_REGISTRY.pop("RoomFree", None)


class TestStaleNodeFilesAreRemoved:
    def test_a_language_change_removes_the_old_language_file_and_its_bytecode(self, tmp_path):
        """NODESYNC-21: created as Python (0-byte .py + __pycache__), saved
        as Bash, renamed, deleted -- the .py and its .pyc stayed forever."""
        engine = _make_engine(tmp_path)
        python_dir = tmp_path / "custom_node_files" / "python"
        engine.sync_custom_node_files({"ward_door_sh": {"language": "python", "source": ""}})
        (python_dir / "__pycache__").mkdir(exist_ok=True)
        (python_dir / "__pycache__" / "ward_door_sh.cpython-312.pyc").write_bytes(b"x")

        engine.sync_custom_node_files({"ward_door_sh": {
            "language": "bash", "source": BASH_NAVIGATE_SOURCE, "xml_tag": "WardDoor"}})
        try:
            assert not (python_dir / "ward_door_sh.py").exists()
            assert not (python_dir / "__pycache__" / "ward_door_sh.cpython-312.pyc").exists()
            assert (tmp_path / "custom_node_files" / "bash" / "ward_door_sh.sh").exists()
        finally:
            NODE_REGISTRY.pop("WardDoor", None)


class TestNodeFileNamesStayInTheirFolder:
    def test_a_traversal_name_is_refused_and_writes_nothing_outside(self, tmp_path):
        """NODESYNC-26: the name came straight from the server into
        os.path.join (custom_aiml already used safe_path_in)."""
        engine = _make_engine(tmp_path)

        failures = engine.sync_custom_node_files({
            "../../escaped": {"language": "bash", "source": BASH_NAVIGATE_SOURCE, "xml_tag": "Escaped"},
        })

        assert not (tmp_path / "escaped.sh").exists()
        assert not (tmp_path.parent / "escaped.sh").exists()
        assert any(f["name"] == "../../escaped" for f in failures)
        assert "Escaped" not in NODE_REGISTRY
        manifest = json.loads((tmp_path / "custom_node_files" / "manifest.json").read_text())
        assert "../../escaped" not in manifest

    def test_no_file_in_the_repo_names_a_local_developer_path(self):
        """NODESYNC-26b: the package source was cleaned, but .gitignore, the
        launch file and ten test files still named a developer's local plan
        file. Every file git would publish is checked (the needles are split
        so this file doesn't match itself)."""
        import pathlib
        import subprocess
        repo = pathlib.Path(__file__).resolve().parent.parent
        try:
            listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=repo,
                                    capture_output=True, text=True)
        except OSError:
            pytest.skip("git isn't installed")
        if listed.returncode != 0:
            pytest.skip("not a git checkout")
        needles = (".claude" + "/plans", "breezy-splashing" + "-koala", "/home/" + "scientist")
        leaks = []
        for name in listed.stdout.splitlines():
            path = repo / name
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_bytes().decode("utf-8", "ignore")
            leaks += [f"{name}: {needle}" for needle in needles if needle in text]
        assert leaks == []


class TestCppNodesAreNotRebuiltNeedlessly:
    """NODESYNC-22: every sync rebuilt every C++ node, and a 120 s g++
    timeout dropped the node's tag until the next sync."""

    def test_an_unchanged_node_is_not_compiled_again(self, tmp_path, monkeypatch):
        import subprocess
        engine = _make_engine(tmp_path)
        payload = {"navigate": {"language": "cpp", "source": CPP_NAVIGATE_SOURCE, "xml_tag": "Navigate"}}
        try:
            assert engine.sync_custom_node_files(payload) == []
            compiles = []
            real_run = subprocess.run
            monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: compiles.append(cmd) or real_run(cmd, *a, **k))

            assert engine.sync_custom_node_files(payload) == []
            engine.sync_custom_node_files()  # the reconnect / startup reload

            assert [c for c in compiles if c and c[0] == "g++"] == []
            assert "Navigate" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("Navigate", None)

    def test_a_failed_rebuild_keeps_the_previous_build_and_its_tag(self, tmp_path, monkeypatch):
        import subprocess
        engine = _make_engine(tmp_path)
        try:
            engine.sync_custom_node_files({"navigate": {"language": "cpp", "source": CPP_NAVIGATE_SOURCE, "xml_tag": "Navigate"}})
            binary = tmp_path / "custom_node_files" / "cpp" / "cpp_build" / "navigate"
            built = binary.read_bytes()

            def timeout(cmd, *a, **k):
                raise subprocess.TimeoutExpired(cmd, 120)
            monkeypatch.setattr(subprocess, "run", timeout)
            failures = engine.sync_custom_node_files({"navigate": {
                "language": "cpp", "source": CPP_NAVIGATE_SOURCE + "\n// v2\n", "xml_tag": "Navigate"}})

            assert "Navigate" in NODE_REGISTRY
            assert binary.read_bytes() == built
            assert len(failures) == 1 and "timed out" in failures[0]["reason"] and "previous build" in failures[0]["reason"]
        finally:
            NODE_REGISTRY.pop("Navigate", None)


class TestCrlfNodeFilesAreNotRewritten:
    def test_an_unchanged_crlf_file_keeps_its_mtime_and_is_not_a_robot_edit(self, tmp_path):
        import os
        engine = _make_engine(tmp_path)
        crlf = ROOM_FREE_SOURCE.replace("\n", "\r\n")
        payload = {"room_free_check": {"language": "python", "source": crlf}}
        try:
            engine.sync_custom_node_files(payload)
            path = tmp_path / "custom_node_files" / "python" / "room_free_check.py"
            os.utime(path, (1_000_000, 1_000_000))

            assert engine.sync_custom_node_files(payload) == []

            assert path.stat().st_mtime == 1_000_000
            assert engine._kept_robot_node_edits == []
        finally:
            NODE_REGISTRY.pop("RoomFree", None)
