"""Bidirectional file sync (see /home/scientist/.claude/plans/
breezy-splashing-koala.md, Part 1): the 8 git-tracked example custom node
files (one Action + one Condition per language) register with ZERO Django
connection -- exactly the real files this repo ships under
custom_behaviors/custom_node_files/, exercised end to end (real subprocess/
compile, matching this repo's own "test against the real thing" convention).

Copies the real examples into a tmp_path first (same reasoning as
test_custom_node_files_sync.py's own _make_engine -- never let a test
write real build artifacts, e.g. cpp_build/, into the actual git checkout).
"""
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


def _make_engine_with_real_examples(tmp_path):
    from xparo.engine import Engine
    dest = tmp_path / 'custom_node_files'
    shutil.copytree(_REAL_EXAMPLES_DIR, dest)
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
