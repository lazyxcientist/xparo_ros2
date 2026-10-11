"""Bidirectional file sync (see /home/scientist/.claude/plans/
breezy-splashing-koala.md, Part 1): the 8 git-tracked example custom node
files (one Action + one Condition per language) register with ZERO Django
connection -- exactly the real files this repo ships under
custom_behaviors/custom_node_files/, exercised end to end (real subprocess/
compile, matching this repo's own "test against the real thing" convention).

Copies the real examples into a tmp_path first (same reasoning as
test_custom_node_files_sync.py's own _make_engine -- never let a test
write real build artifacts, e.g. cpp_build/, into the actual git checkout)
-- and, since custom_node_files/ now also collects real, gitignored,
non-deterministic state right alongside the examples (manifest.json,
cpp_build/, js_runtime/, __pycache__/, and this checkout's own real,
sometimes-mid-edit CustomFile-backed sources), copies ONLY the exact
files examples_manifest.json itself declares -- never a whole-directory
copytree -- so this test stays hermetic no matter what real content
someone's actively testing with happens to be sitting in that folder.
"""
import json
import os
import shutil

import pytest
from py_trees import common

from xparo.bt_engine.node_registry import NODE_REGISTRY

_REAL_EXAMPLES_DIR = os.path.join(
    os.path.dirname(__file__), '..', 'custom_behaviors', 'custom_node_files',
)

_EXAMPLE_TAGS = [
    "GreetExample", "BatteryOkExample",
    "GreetExampleCpp", "BatteryOkExampleCpp",
    "GreetExampleJs", "BatteryOkExampleJs",
    "GreetExampleBash", "BatteryOkExampleBash",
]

_EXTENSIONS_BY_LANGUAGE = {"python": ".py", "cpp": ".cpp", "javascript": ".js", "bash": ".sh"}


def _make_engine_with_real_examples(tmp_path):
    from xparo.engine import Engine
    dest = tmp_path / 'custom_node_files'
    dest.mkdir()
    real_manifest_path = os.path.join(_REAL_EXAMPLES_DIR, 'examples_manifest.json')
    shutil.copy(real_manifest_path, dest / 'examples_manifest.json')
    with open(real_manifest_path, 'r') as file:
        examples_manifest = json.load(file)
    for language, entries in examples_manifest.items():
        language_dir = dest / language
        language_dir.mkdir()
        for name in entries:
            filename = name + _EXTENSIONS_BY_LANGUAGE[language]
            shutil.copy(os.path.join(_REAL_EXAMPLES_DIR, language, filename), language_dir / filename)
    engine = Engine("secret", "proj-custom-node-file-examples-test", connection_type="offline")
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    return engine


@pytest.fixture(autouse=True)
def _clean_registry():
    for tag in _EXAMPLE_TAGS:
        NODE_REGISTRY.pop(tag, None)
    yield
    for tag in _EXAMPLE_TAGS:
        NODE_REGISTRY.pop(tag, None)


class TestCustomNodeFilesExamples:
    def test_all_eight_examples_register_with_no_django_payload_at_all(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)

        failures = engine.sync_custom_node_files(None)

        assert failures == []
        for tag in _EXAMPLE_TAGS:
            assert tag in NODE_REGISTRY, f"{tag} did not register from the real example files"

    def test_the_python_greet_example_actually_runs_and_writes_its_output(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)
        engine.sync_custom_node_files(None)

        from xparo.bt_engine import tree_builder
        import py_trees
        blackboard = {}
        root = tree_builder.build_tree('<GreetExample name="tester" greeting="out.greeting" />', blackboard)
        tree = py_trees.trees.BehaviourTree(root)
        tree.tick()
        assert root.status == common.Status.SUCCESS
        assert blackboard.get("out.greeting") == "Hello, tester! XPARO custom node pipeline is working."

    def test_the_python_battery_ok_example_succeeds_below_the_simulated_level(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)
        engine.sync_custom_node_files(None)

        from xparo.bt_engine import tree_builder
        import py_trees
        root = tree_builder.build_tree('<BatteryOkExample min_level="20" />', {})
        tree = py_trees.trees.BehaviourTree(root)
        tree.tick()
        assert root.status == common.Status.SUCCESS

    def test_the_python_battery_ok_example_fails_above_the_simulated_level(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)
        engine.sync_custom_node_files(None)

        from xparo.bt_engine import tree_builder
        import py_trees
        root = tree_builder.build_tree('<BatteryOkExample min_level="99" />', {})
        tree = py_trees.trees.BehaviourTree(root)
        tree.tick()
        assert root.status == common.Status.FAILURE

    def test_the_cpp_greet_example_actually_compiles_and_runs(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)
        engine.sync_custom_node_files(None)

        from xparo.bt_engine import tree_builder
        import py_trees
        root = tree_builder.build_tree('<GreetExampleCpp name="tester" />', {})
        tree = py_trees.trees.BehaviourTree(root)
        tree.tick()
        assert root.status == common.Status.SUCCESS

    def test_the_js_greet_example_actually_runs(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)
        engine.sync_custom_node_files(None)

        from xparo.bt_engine import tree_builder
        import py_trees
        root = tree_builder.build_tree('<GreetExampleJs name="tester" />', {})
        tree = py_trees.trees.BehaviourTree(root)
        tree.tick()
        assert root.status == common.Status.SUCCESS

    def test_the_bash_greet_example_actually_runs(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)
        engine.sync_custom_node_files(None)

        from xparo.bt_engine import tree_builder
        import py_trees
        root = tree_builder.build_tree('<GreetExampleBash name="tester" />', {})
        tree = py_trees.trees.BehaviourTree(root)
        tree.tick()
        assert root.status == common.Status.SUCCESS

    def test_a_django_synced_file_with_the_same_name_shadows_the_example_and_is_logged(self, tmp_path):
        engine = _make_engine_with_real_examples(tmp_path)

        failures = engine.sync_custom_node_files({
            "greet_example": {
                "language": "python", "source": "from py_trees.common import Status\nfrom xparo.bt_engine.plugin_loader import CustomBTNode\n\n\nclass Overridden(CustomBTNode):\n    XML_TAG = \"Overridden\"\n\n    def update(self):\n        return Status.SUCCESS\n",
                "xml_tag": "Overridden", "node_type": "action", "ports": [],
            },
        })

        assert "Overridden" in NODE_REGISTRY
        # The example's own tag never registered -- the real file with the
        # same on-disk name won, matching production CustomFile's own
        # per-project name-uniqueness constraint.
        assert "GreetExample" not in NODE_REGISTRY
        NODE_REGISTRY.pop("Overridden", None)



class TestResyncKeepsNodesAvailable:
    """Confirmed live on a real robot: every (re)connect re-syncs custom
    nodes, and that used to unregister them all before re-registering --
    recompiling each C++ node in between -- so a task arriving meanwhile
    failed with "<greet_example_cpp> isn't a node this robot knows".
    Self-contained (own bash + faked-compile C++ node), no example files."""

    TAGS = ["ResyncBashNode", "ResyncCppNode"]
    PAYLOAD = {
        "resync_bash": {"language": "bash", "xml_tag": "ResyncBashNode", "node_type": "action",
                        "source": "#!/usr/bin/env bash\nexit 0\n", "ports": []},
        "resync_cpp": {"language": "cpp", "xml_tag": "ResyncCppNode", "node_type": "action",
                       "source": "// never really compiled", "ports": []},
    }

    @pytest.fixture(autouse=True)
    def _registry(self):
        for tag in self.TAGS:
            NODE_REGISTRY.pop(tag, None)
        yield
        for tag in self.TAGS:
            NODE_REGISTRY.pop(tag, None)

    def _engine(self, tmp_path):
        from xparo.engine import Engine
        engine = Engine("secret", "proj-resync-test", connection_type="offline")
        engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
        return engine

    def test_nothing_already_registered_disappears_while_a_resync_compiles(self, tmp_path):
        from unittest.mock import patch
        from xparo.bt_engine import runners
        engine = self._engine(tmp_path)
        with patch.object(runners, 'compile_cpp_node', return_value=("/bin/true", None)):
            assert engine.sync_custom_node_files(self.PAYLOAD) == []
        assert all(tag in NODE_REGISTRY for tag in self.TAGS)

        missing_during_compile = []

        def watching_compile(*args, **kwargs):
            missing_during_compile.extend(t for t in self.TAGS if t not in NODE_REGISTRY)
            return "/bin/true", None

        with patch.object(runners, 'compile_cpp_node', side_effect=watching_compile):
            assert engine.sync_custom_node_files(self.PAYLOAD) == []

        assert missing_during_compile == []
        assert all(tag in NODE_REGISTRY for tag in self.TAGS)

    def test_a_node_that_was_removed_is_unregistered_after_the_resync(self, tmp_path):
        from unittest.mock import patch
        from xparo.bt_engine import runners
        engine = self._engine(tmp_path)
        with patch.object(runners, 'compile_cpp_node', return_value=("/bin/true", None)):
            engine.sync_custom_node_files(self.PAYLOAD)
            assert "ResyncCppNode" in NODE_REGISTRY
            engine.sync_custom_node_files({"resync_bash": self.PAYLOAD["resync_bash"]})
        assert "ResyncBashNode" in NODE_REGISTRY
        assert "ResyncCppNode" not in NODE_REGISTRY
