"""Bidirectional file sync (see /home/scientist/.claude/plans/breezy-
splashing-koala.md): the robot-side half of "why am I not able to see
already-added files in xparo_api got syncing with behaviour tree also" --
a file dropped straight into custom_node_files/<language>/ (never synced
from Django, not one of the shipped examples) previously never showed up
in LOCAL_FILE_STATE/LOCAL_FILE_CONTENT at all, so Django never had a
chance to adopt it. _looks_like_a_node_file/_discover_new_node_files/
_detect_node_metadata close that gap, deliberately excluding importable/
include-only files per the user's own "no need to sync its importable
files, or include files, just main file" instruction.

Same _make_engine(tmp_path) convention as test_local_file_state.py
(overrides both folder paths so a test can never write into this repo's
own real, git-tracked custom_behaviors/custom_envs).
"""
import json


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-new-node-discovery-test", **kwargs)
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path / "custom_behaviors")
    engine.files["xparo_custom_evns_folder_path"] = str(tmp_path / "custom_envs")
    return engine


PYTHON_NODE_SOURCE = (
    "from xparo.bt_engine.plugin_loader import CustomBTNode\n"
    "from py_trees.common import Status\n\n\n"
    "class GreetExample(CustomBTNode):\n"
    "    XML_TAG = \"GreetExample\"\n\n"
    "    def update(self):\n"
    "        return Status.SUCCESS\n"
)

PYTHON_HELPER_SOURCE = (
    "def double(x):\n"
    "    return x * 2\n"
)

CPP_ACTION_SOURCE = (
    "#include \"behaviortree_cpp/bt_factory.h\"\n\n"
    "class NavigateCpp : public BT::SyncActionNode {\n"
    "public:\n"
    "  NavigateCpp(const std::string& name, const BT::NodeConfig& config)\n"
    "      : BT::SyncActionNode(name, config) {}\n"
    "  static BT::PortsList providedPorts() {\n"
    "    return {BT::InputPort<std::string>(\"destination\"), BT::OutputPort<bool>(\"arrived\")};\n"
    "  }\n"
    "  BT::NodeStatus tick() override { return BT::NodeStatus::SUCCESS; }\n"
    "};\n"
)

CPP_CONDITION_SOURCE = (
    "class BatteryOkCpp : public BT::ConditionNode {\n"
    "public:\n"
    "  BatteryOkCpp(const std::string& name, const BT::NodeConfig& config)\n"
    "      : BT::ConditionNode(name, config) {}\n"
    "  BT::NodeStatus tick() override { return BT::NodeStatus::SUCCESS; }\n"
    "};\n"
)

CPP_HEADER_ONLY_SOURCE = (
    "#pragma once\n"
    "inline int helper_add(int a, int b) { return a + b; }\n"
)

JS_NODE_SOURCE = (
    "class GreetJs extends XparoNode {\n"
    "  tick() {\n"
    "    this.output(\"greeting\", \"hi\");\n"
    "    return \"SUCCESS\";\n"
    "  }\n"
    "}\n"
    "module.exports = GreetJs;\n"
)

JS_HELPER_SOURCE = (
    "function double(x) { return x * 2; }\n"
    "module.exports = { double };\n"
)

BASH_SOURCE = (
    "#!/bin/bash\n"
    "DESTINATION=${DESTINATION:-home}\n"
    "echo \"ARRIVED=true\"\n"
)


class TestLooksLikeANodeFile:
    def test_a_real_python_custom_bt_node_subclass_counts(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine._looks_like_a_node_file("python", PYTHON_NODE_SOURCE) is True

    def test_a_plain_python_helper_with_no_node_class_does_not_count(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine._looks_like_a_node_file("python", PYTHON_HELPER_SOURCE) is False

    def test_a_cpp_action_or_condition_class_counts(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine._looks_like_a_node_file("cpp", CPP_ACTION_SOURCE) is True
        assert engine._looks_like_a_node_file("cpp", CPP_CONDITION_SOURCE) is True

    def test_a_cpp_header_with_no_node_class_does_not_count(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine._looks_like_a_node_file("cpp", CPP_HEADER_ONLY_SOURCE) is False

    def test_a_javascript_xparo_node_subclass_counts(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine._looks_like_a_node_file("javascript", JS_NODE_SOURCE) is True

    def test_a_plain_javascript_helper_does_not_count(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine._looks_like_a_node_file("javascript", JS_HELPER_SOURCE) is False

    def test_bash_always_counts_since_it_has_no_include_convention(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine._looks_like_a_node_file("bash", "#!/bin/bash\necho hi\n") is True


class TestDiscoverNewNodeFiles:
    def test_finds_a_brand_new_python_node_file_not_in_known_names(self, tmp_path):
        engine = _make_engine(tmp_path)
        python_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "python"
        python_dir.mkdir(parents=True)
        (python_dir / "greet_example.py").write_text(PYTHON_NODE_SOURCE)

        discovered = engine._discover_new_node_files(known_names=set())

        assert discovered["greet_example"]["language"] == "python"
        assert discovered["greet_example"]["source"] == PYTHON_NODE_SOURCE

    def test_skips_a_helper_file_with_no_node_class(self, tmp_path):
        engine = _make_engine(tmp_path)
        python_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "python"
        python_dir.mkdir(parents=True)
        (python_dir / "helpers.py").write_text(PYTHON_HELPER_SOURCE)

        discovered = engine._discover_new_node_files(known_names=set())

        assert "helpers" not in discovered

    def test_skips_a_name_already_in_known_names(self, tmp_path):
        engine = _make_engine(tmp_path)
        python_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "python"
        python_dir.mkdir(parents=True)
        (python_dir / "greet_example.py").write_text(PYTHON_NODE_SOURCE)

        discovered = engine._discover_new_node_files(known_names={"greet_example"})

        assert discovered == {}

    def test_a_cpp_header_only_include_file_is_never_reported_as_a_main_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        cpp_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "cpp"
        cpp_dir.mkdir(parents=True)
        (cpp_dir / "helper_only.hpp").write_text(CPP_HEADER_ONLY_SOURCE)

        discovered = engine._discover_new_node_files(known_names=set())

        assert discovered == {}

    def test_a_cpp_main_file_picks_up_its_matching_header_as_header_source(self, tmp_path):
        engine = _make_engine(tmp_path)
        cpp_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "cpp"
        cpp_dir.mkdir(parents=True)
        (cpp_dir / "navigate_cpp.cpp").write_text(CPP_ACTION_SOURCE)
        (cpp_dir / "navigate_cpp.hpp").write_text("#pragma once\n")

        discovered = engine._discover_new_node_files(known_names=set())

        assert discovered["navigate_cpp"]["header_source"] == "#pragma once\n"


class TestDetectNodeMetadata:
    def test_python_metadata_uses_the_real_xml_tag_via_introspection(self, tmp_path):
        engine = _make_engine(tmp_path)
        metadata = engine._detect_node_metadata("greet_example", "python", PYTHON_NODE_SOURCE)
        assert metadata["xml_tag"] == "GreetExample"
        assert metadata["node_type"] == "action"
        assert metadata["ports"] == []

    def test_cpp_action_metadata_derives_class_name_and_ports(self, tmp_path):
        engine = _make_engine(tmp_path)
        metadata = engine._detect_node_metadata("navigate_cpp", "cpp", CPP_ACTION_SOURCE)
        assert metadata["xml_tag"] == "NavigateCpp"
        assert metadata["node_type"] == "action"
        assert {"direction": "input", "key": "destination"} in metadata["ports"]
        assert {"direction": "output", "key": "arrived"} in metadata["ports"]

    def test_cpp_condition_metadata_is_detected_as_condition_node_type(self, tmp_path):
        engine = _make_engine(tmp_path)
        metadata = engine._detect_node_metadata("battery_ok_cpp", "cpp", CPP_CONDITION_SOURCE)
        assert metadata["xml_tag"] == "BatteryOkCpp"
        assert metadata["node_type"] == "condition"

    def test_javascript_metadata_derives_class_name_and_ports(self, tmp_path):
        engine = _make_engine(tmp_path)
        metadata = engine._detect_node_metadata("greet_js", "javascript", JS_NODE_SOURCE)
        assert metadata["xml_tag"] == "GreetJs"
        assert {"direction": "output", "key": "greeting"} in metadata["ports"]

    def test_bash_metadata_falls_back_to_pascal_case_name_with_convention_based_ports(self, tmp_path):
        engine = _make_engine(tmp_path)
        metadata = engine._detect_node_metadata("greet_example_bash", "bash", BASH_SOURCE)
        assert metadata["xml_tag"] == "GreetExampleBash"
        assert {"direction": "input", "key": "destination"} in metadata["ports"]
        assert {"direction": "output", "key": "arrived"} in metadata["ports"]


class TestGetLocalFileStateIncludesNewlyDiscoveredFiles:
    def test_a_dropped_in_python_node_file_shows_up_in_local_file_state(self, tmp_path):
        engine = _make_engine(tmp_path)
        python_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "python"
        python_dir.mkdir(parents=True)
        (python_dir / "greet_example.py").write_text(PYTHON_NODE_SOURCE)

        state = engine.get_local_file_state()

        assert "greet_example" in state["custom_node_files"]
        assert state["custom_node_files"]["greet_example"]["language"] == "python"

    def test_a_file_already_in_manifest_json_is_not_duplicated_as_new(self, tmp_path):
        engine = _make_engine(tmp_path)
        node_files_dir = tmp_path / "custom_behaviors" / "custom_node_files"
        (node_files_dir / "python").mkdir(parents=True)
        (node_files_dir / "python" / "navigate.py").write_text(PYTHON_NODE_SOURCE)
        (node_files_dir / "manifest.json").write_text(json.dumps({
            "navigate": {"language": "python", "xml_tag": "Navigate", "node_type": "action", "ports": [], "header_source": ""},
        }))

        state = engine.get_local_file_state()

        assert list(state["custom_node_files"].keys()).count("navigate") == 1


class TestGetLocalFileContentForNewlyDiscoveredFiles:
    def test_a_name_not_in_manifest_gets_source_plus_detected_metadata_hints(self, tmp_path):
        engine = _make_engine(tmp_path)
        python_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "python"
        python_dir.mkdir(parents=True)
        (python_dir / "greet_example.py").write_text(PYTHON_NODE_SOURCE)

        response = engine.get_local_file_content({"custom_node_files": ["greet_example"]})

        entry = response["custom_node_files"]["greet_example"]
        assert entry["source"] == PYTHON_NODE_SOURCE
        assert entry["language"] == "python"
        assert entry["detected_xml_tag"] == "GreetExample"
        assert entry["detected_node_type"] == "action"
        assert entry["detected_ports"] == []

    def test_a_known_manifest_entry_never_carries_detected_metadata_hints(self, tmp_path):
        engine = _make_engine(tmp_path)
        node_files_dir = tmp_path / "custom_behaviors" / "custom_node_files"
        (node_files_dir / "python").mkdir(parents=True)
        (node_files_dir / "python" / "navigate.py").write_text(PYTHON_NODE_SOURCE)
        (node_files_dir / "manifest.json").write_text(json.dumps({
            "navigate": {"language": "python", "xml_tag": "Navigate", "node_type": "action", "ports": [], "header_source": ""},
        }))

        response = engine.get_local_file_content({"custom_node_files": ["navigate"]})

        entry = response["custom_node_files"]["navigate"]
        assert "detected_xml_tag" not in entry
        assert "language" not in entry

    def test_a_helper_only_file_with_no_node_class_is_not_offered_at_all(self, tmp_path):
        engine = _make_engine(tmp_path)
        python_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "python"
        python_dir.mkdir(parents=True)
        (python_dir / "helpers.py").write_text(PYTHON_HELPER_SOURCE)

        response = engine.get_local_file_content({"custom_node_files": ["helpers"]})

        assert "helpers" not in response["custom_node_files"]
