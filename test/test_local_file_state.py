"""Bidirectional file sync: get_local_files()'s new pure-read
behavior (no more private_send side effect) and get_local_file_state()'s
hash-only LOCAL_FILE_STATE shape, including bootstrap-on-read for
custom_aiml/custom_maps entries with no prior sync_state.json.

_make_engine here overrides BOTH xparo_custom_behaviors_folder_path and
xparo_custom_evns_folder_path off tmp_path -- unlike
test_custom_node_files_sync.py's own _make_engine (which only needs the
first), get_local_file_state also touches custom_envs/custom_maps, and
that root defaults to a real, git-tracked directory in this checkout
(confirmed: custom_envs/hi.env is a tracked file) that a test must never
write into.
"""
import json

from xparo.sync_hash import content_hash


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-local-file-state-test", **kwargs)
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path / "custom_behaviors")
    engine.files["xparo_custom_evns_folder_path"] = str(tmp_path / "custom_envs")
    return engine


class TestGetLocalFilesIsAPureRead:
    def test_returns_a_dict_and_causes_no_network_send(self, tmp_path):
        engine = _make_engine(tmp_path)
        sent = []
        engine.private_send = lambda *a, **k: sent.append(a)

        result = engine.get_local_files()

        assert isinstance(result, dict)
        assert "custom_aiml" in result
        assert sent == []  # the old private_send({"save_aiml": ...}) side effect is gone


class TestGetLocalFileState:
    def test_reports_a_hash_not_raw_content_for_a_custom_aiml_entry(self, tmp_path):
        engine = _make_engine(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir(parents=True)
        (aiml_dir / "quick_delivery_tree.xml").write_text(
            '<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
            '<BehaviorTree ID="MainTree">\n<Sequence></Sequence>\n</BehaviorTree>\n</root>'
        )

        state = engine.get_local_file_state()

        assert state["device_id"] == engine.local_database.unique_id
        entry = state["custom_aiml"]["quick_delivery_tree"]
        assert entry["content_hash"] == content_hash("<Sequence></Sequence>")
        assert "source" not in entry and "content" not in entry  # hash-only, not full content

    def test_reports_a_hash_for_a_custom_maps_entry(self, tmp_path):
        engine = _make_engine(tmp_path)
        maps_dir = tmp_path / "custom_envs" / "custom_maps"
        maps_dir.mkdir(parents=True)
        (maps_dir / "robot.env").write_text("KEY=value\n")

        state = engine.get_local_file_state()

        # get_local_files strips the .env extension for the dict key
        # (filename[:-4]), matching custom_aiml's own .xml-stripping
        # convention -- "robot.env" on disk becomes key "robot".
        assert state["custom_maps"]["robot"]["content_hash"] == content_hash("KEY=value\n")

    def test_bootstraps_a_sync_state_baseline_for_a_pre_existing_custom_aiml_file(self, tmp_path):
        """Part 5: a file that predates sync_state.json tracking gets a
        baseline computed from its current content, written back, and is
        never treated as a conflict just because tracking is new."""
        engine = _make_engine(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir(parents=True)
        (aiml_dir / "old_tree.xml").write_text(
            '<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
            '<BehaviorTree ID="MainTree">\n<Fallback></Fallback>\n</BehaviorTree>\n</root>'
        )
        assert not (aiml_dir / "sync_state.json").exists()

        state = engine.get_local_file_state()

        assert state["custom_aiml"]["old_tree"]["content_hash"] == content_hash("<Fallback></Fallback>")
        persisted = json.loads((aiml_dir / "sync_state.json").read_text())
        assert persisted["old_tree"]["hash"] == content_hash("<Fallback></Fallback>")
        assert persisted["old_tree"]["synced_at"]

    def test_includes_custom_node_files_hashes_a_gap_the_old_mechanism_never_covered(self, tmp_path):
        engine = _make_engine(tmp_path)
        node_files_dir = tmp_path / "custom_behaviors" / "custom_node_files" / "python"
        node_files_dir.mkdir(parents=True)
        (node_files_dir / "navigate.py").write_text("print('hi')\n")
        manifest_path = tmp_path / "custom_behaviors" / "custom_node_files" / "manifest.json"
        manifest_path.write_text(json.dumps({
            "navigate": {"language": "python", "xml_tag": "", "node_type": "action", "ports": [], "header_source": ""},
        }))

        state = engine.get_local_file_state()

        assert state["custom_node_files"]["navigate"]["content_hash"] == content_hash("print('hi')\n", "")
        assert state["custom_node_files"]["navigate"]["language"] == "python"

class TestGetLocalFileContent:
    def test_returns_source_and_header_source_for_a_requested_custom_node_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        node_dir = tmp_path / "custom_behaviors" / "custom_node_files"
        (node_dir / "cpp").mkdir(parents=True)
        (node_dir / "cpp" / "navigate.cpp").write_text("// real source\n")
        (node_dir / "manifest.json").write_text(json.dumps({
            "navigate": {"language": "cpp", "xml_tag": "Navigate", "node_type": "action", "ports": [], "header_source": "#pragma once\n"},
        }))

        response = engine.get_local_file_content({"custom_node_files": ["navigate"]})

        assert response["device_id"] == engine.local_database.unique_id
        assert response["custom_node_files"]["navigate"]["source"] == "// real source\n"
        assert response["custom_node_files"]["navigate"]["header_source"] == "#pragma once\n"

    def test_returns_the_inner_tree_content_for_a_requested_custom_aiml_entry(self, tmp_path):
        engine = _make_engine(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir(parents=True)
        (aiml_dir / "tree.xml").write_text(
            '<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
            '<BehaviorTree ID="MainTree">\n<Sequence></Sequence>\n</BehaviorTree>\n</root>'
        )

        response = engine.get_local_file_content({"custom_aiml": ["tree"]})

        assert response["custom_aiml"]["tree"] == "<Sequence></Sequence>"

    def test_returns_raw_content_for_a_requested_custom_maps_entry(self, tmp_path):
        engine = _make_engine(tmp_path)
        maps_dir = tmp_path / "custom_envs" / "custom_maps"
        maps_dir.mkdir(parents=True)
        (maps_dir / "robot.env").write_text("KEY=value\n")

        response = engine.get_local_file_content({"custom_maps": ["robot"]})

        assert response["custom_maps"]["robot"] == "KEY=value\n"

    def test_a_name_missing_on_disk_is_simply_omitted_not_an_error(self, tmp_path):
        engine = _make_engine(tmp_path)

        response = engine.get_local_file_content({
            "custom_node_files": ["ghost"], "custom_aiml": ["ghost"], "custom_maps": ["ghost"],
        })

        assert response["custom_node_files"] == {}
        assert response["custom_aiml"] == {}
        assert response["custom_maps"] == {}

    def test_on_ws_message_request_local_file_content_sends_local_file_content_back(self, tmp_path):
        engine = _make_engine(tmp_path)
        maps_dir = tmp_path / "custom_envs" / "custom_maps"
        maps_dir.mkdir(parents=True)
        (maps_dir / "robot.env").write_text("KEY=value\n")
        sent = []
        engine.transport.send = lambda message, command_for=None: sent.append(json.loads(message))

        engine.on_ws_message('ws', {"REQUEST_LOCAL_FILE_CONTENT": {"custom_maps": ["robot"]}})

        payloads = [m["LOCAL_FILE_CONTENT"] for m in sent if "LOCAL_FILE_CONTENT" in m]
        assert len(payloads) == 1
        assert payloads[0]["custom_maps"]["robot"] == "KEY=value\n"


class TestGetLocalFilesSyncStateNotRewritten:
    def test_a_file_already_tracked_in_sync_state_is_not_rewritten_needlessly(self, tmp_path):
        engine = _make_engine(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir(parents=True)
        (aiml_dir / "tree.xml").write_text(
            '<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
            '<BehaviorTree ID="MainTree">\n<Sequence></Sequence>\n</BehaviorTree>\n</root>'
        )
        existing_state = {"tree": {"hash": content_hash("<Sequence></Sequence>"), "synced_at": "2020-01-01T00:00:00"}}
        (aiml_dir / "sync_state.json").write_text(json.dumps(existing_state))

        engine.get_local_file_state()

        persisted = json.loads((aiml_dir / "sync_state.json").read_text())
        assert persisted["tree"]["synced_at"] == "2020-01-01T00:00:00"  # untouched, not re-stamped


# MEG-12: a Groot2 save found on a robot -> "Import into project" sent it to
# the server whole, the server sent it back, and the robot wrapped it in a
# second <root><BehaviorTree ID="MainTree">, putting the <?xml?> prolog on
# line 3 ("XML or text declaration not at start of entity").
GROOT2_DOC = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<root BTCPP_format="4">\n'
    '  <BehaviorTree ID="MedDelivery">\n'
    '    <Sequence><SubTree ID="GoToRoom" goal="{room}"/><Wait name="डिलीवरी"/></Sequence>\n'
    '  </BehaviorTree>\n'
    '  <BehaviorTree ID="GoToRoom">\n'
    '    <RoomIsFree room="{goal}"/>\n'
    '  </BehaviorTree>\n'
    '  <TreeNodesModel><Condition ID="RoomIsFree"><input_port name="room"/></Condition></TreeNodesModel>\n'
    '</root>\n'
)


class TestWholeTreeDocumentsAreKeptAsTheyAre:
    def test_a_synced_groot2_document_is_written_exactly_as_it_is(self, tmp_path):
        import xml.etree.ElementTree as ET
        engine = _make_engine(tmp_path)

        engine.on_ws_message('ws', {"custom_aiml": {"med_delivery_groot": GROOT2_DOC}})

        written = (tmp_path / "custom_behaviors" / "custom_aiml" / "med_delivery_groot.xml").read_text()
        assert written == GROOT2_DOC
        assert [t.get("ID") for t in ET.fromstring(written).iter("BehaviorTree")] == ["MedDelivery", "GoToRoom"]

    def test_importing_a_file_found_on_the_robot_leaves_the_robots_file_untouched(self, tmp_path):
        import os
        engine = _make_engine(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir(parents=True)
        robot_file = aiml_dir / "ward_run.xml"
        ward_run = '<root BTCPP_format="4">\n<BehaviorTree ID="ward_run">\n<AlwaysSuccess/>\n</BehaviorTree>\n</root>\n'
        robot_file.write_text(ward_run)
        os.utime(robot_file, (1_000_000, 1_000_000))

        sent_up = engine.get_local_file_content({"custom_aiml": ["ward_run"]})["custom_aiml"]["ward_run"]
        engine.on_ws_message('ws', {"custom_aiml": {"ward_run": sent_up}})  # the server's push after the import

        assert sent_up == ward_run
        assert robot_file.read_text() == ward_run
        assert robot_file.stat().st_mtime == 1_000_000

    def test_a_document_whose_main_tree_is_maintree_is_sent_whole_with_its_subtrees(self, tmp_path):
        engine = _make_engine(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir(parents=True)
        doc = ('<root BTCPP_format="4" main_tree_to_execute="MainTree">\n'
               '<BehaviorTree ID="MainTree">\n<SubTree ID="Dock"/>\n</BehaviorTree>\n'
               '<BehaviorTree ID="Dock">\n<AlwaysSuccess/>\n</BehaviorTree>\n</root>')
        (aiml_dir / "two_trees.xml").write_text(doc)

        response = engine.get_local_file_content({"custom_aiml": ["two_trees"]})

        assert response["custom_aiml"]["two_trees"] == doc  # not cut down to MainTree's body

    def test_a_file_wrapped_twice_before_the_fix_reads_back_as_the_original_document(self, tmp_path):
        engine = _make_engine(tmp_path)
        aiml_dir = tmp_path / "custom_behaviors" / "custom_aiml"
        aiml_dir.mkdir(parents=True)
        (aiml_dir / "med_delivery_groot.xml").write_text(
            f'<root BTCPP_format="4" main_tree_to_execute="MainTree">\n<BehaviorTree ID="MainTree">\n'
            f'{GROOT2_DOC}\n</BehaviorTree>\n</root>'
        )

        response = engine.get_local_file_content({"custom_aiml": ["med_delivery_groot"]})

        assert response["custom_aiml"]["med_delivery_groot"] == GROOT2_DOC.strip()

    def test_a_plain_fragment_is_still_wrapped(self, tmp_path):
        engine = _make_engine(tmp_path)

        engine.on_ws_message('ws', {"custom_aiml": {"t": "<Sequence></Sequence>"}})

        written = (tmp_path / "custom_behaviors" / "custom_aiml" / "t.xml").read_text()
        assert written.startswith('<root BTCPP_format="4" main_tree_to_execute="MainTree">\n<BehaviorTree ID="MainTree">\n')
        assert engine.get_local_file_content({"custom_aiml": ["t"]})["custom_aiml"]["t"] == "<Sequence></Sequence>"

    def test_a_task_on_a_groot2_tree_runs_it_with_its_subtrees(self, tmp_path):
        from xparo.bt_engine import task_sync
        engine = _make_engine(tmp_path)
        engine.on_ws_message('ws', {"custom_aiml": {"med": GROOT2_DOC}})

        tree_xml = task_sync.resolve_tree_xml("med", engine.files)

        assert tree_xml == GROOT2_DOC

    def test_a_document_saved_with_a_utf8_bom_is_not_wrapped(self, tmp_path):
        engine = _make_engine(tmp_path)

        engine.on_ws_message('ws', {"custom_aiml": {"bom_doc": "﻿" + GROOT2_DOC}})

        written = (tmp_path / "custom_behaviors" / "custom_aiml" / "bom_doc.xml").read_text()
        assert written == "﻿" + GROOT2_DOC
