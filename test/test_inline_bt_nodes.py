"""Behaviour Tree redesign Phase 13 (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): inline custom-node code authored in the BT
editor, shipped to and run on the robot. Reuses Phase 12's
plugin_loader.register_plugins() against a custom_behaviors/inline_nodes/
folder instead of an arbitrary path -- these tests cover the sync/persist/
delete mechanics that are new here; loader correctness itself is already
covered by test_plugin_loader.py.
"""
import json

from xparo.bt_engine.node_registry import NODE_REGISTRY

INLINE_NODE_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class GreetPatient(CustomBTNode):
    XML_TAG = "GreetPatient"

    def update(self):
        return common.Status.SUCCESS
'''

OTHER_INLINE_NODE_SOURCE = '''
from xparo.bt_engine.plugin_loader import CustomBTNode
from py_trees import common


class WaveGoodbye(CustomBTNode):
    XML_TAG = "WaveGoodbye"

    def update(self):
        return common.Status.SUCCESS
'''


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-inline-node-sync-test", **kwargs)
    # Same reasoning as test_plugin_loader.py's _make_engine -- redirect
    # off the real repo checkout, which sync_bt_inline_nodes would
    # otherwise really write .py files into.
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    return engine


class TestEngineSyncBtInlineNodes:
    def test_resync_persists_one_file_per_node_and_registers_it(self, tmp_path):
        engine = _make_engine(tmp_path)

        try:
            engine.sync_bt_inline_nodes({"GreetPatient": INLINE_NODE_SOURCE})

            written = (tmp_path / "inline_nodes" / "GreetPatient.py").read_text()
            assert written == INLINE_NODE_SOURCE
            assert "GreetPatient" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("GreetPatient", None)

    def test_a_node_removed_from_the_dict_has_its_file_deleted_and_is_unregistered(self, tmp_path):
        engine = _make_engine(tmp_path)
        NODE_REGISTRY.pop("GreetPatient", None)

        try:
            engine.sync_bt_inline_nodes({"GreetPatient": INLINE_NODE_SOURCE, "WaveGoodbye": OTHER_INLINE_NODE_SOURCE})
            assert (tmp_path / "inline_nodes" / "GreetPatient.py").exists()

            engine.sync_bt_inline_nodes({"WaveGoodbye": OTHER_INLINE_NODE_SOURCE})

            assert not (tmp_path / "inline_nodes" / "GreetPatient.py").exists()
            assert "GreetPatient" not in NODE_REGISTRY
            assert "WaveGoodbye" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("GreetPatient", None)
            NODE_REGISTRY.pop("WaveGoodbye", None)

    def test_an_updated_source_is_rewritten_and_reregistered(self, tmp_path):
        engine = _make_engine(tmp_path)

        try:
            engine.sync_bt_inline_nodes({"GreetPatient": INLINE_NODE_SOURCE})
            first_builder = NODE_REGISTRY["GreetPatient"]

            updated_source = INLINE_NODE_SOURCE.replace("SUCCESS", "FAILURE")
            engine.sync_bt_inline_nodes({"GreetPatient": updated_source})

            assert (tmp_path / "inline_nodes" / "GreetPatient.py").read_text() == updated_source
            assert NODE_REGISTRY["GreetPatient"] is not first_builder
        finally:
            NODE_REGISTRY.pop("GreetPatient", None)

    def test_startup_with_no_persisted_folder_is_a_safe_noop(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.sync_bt_inline_nodes()  # no inline_nodes/ folder exists yet -- must not raise

    def test_startup_loads_whatever_was_persisted_by_an_earlier_resync(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.sync_bt_inline_nodes({"GreetPatient": INLINE_NODE_SOURCE})
        NODE_REGISTRY.pop("GreetPatient", None)  # simulate a fresh process that hasn't loaded it yet

        try:
            fresh_engine = _make_engine(tmp_path)
            fresh_engine.sync_bt_inline_nodes()  # startup case -- reads the files the resync above wrote

            assert "GreetPatient" in NODE_REGISTRY
        finally:
            NODE_REGISTRY.pop("GreetPatient", None)


class TestOnWsMessageInlineNodeSync:
    def test_custom_bt_node_inline_code_key_dispatches_to_sync_bt_inline_nodes(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.transport.send = lambda message, command_for=None: None

        try:
            engine.on_ws_message('ws', {"custom_bt_node_inline_code": {"GreetPatient": INLINE_NODE_SOURCE}})
            assert "GreetPatient" in NODE_REGISTRY
            assert (tmp_path / "inline_nodes" / "GreetPatient.py").exists()
        finally:
            NODE_REGISTRY.pop("GreetPatient", None)
