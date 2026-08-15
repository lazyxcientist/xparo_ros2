"""ROS bag recording config -- synced from Project_Dashboard (apps/
analytics/data_analyis.py's GET_rosbag_config/EDIT_rosbag_config) down to
this robot. Two readers of the same persisted file
(custom_behaviors/rosbag_config.json): xparo_ros.py reads start_mode/
start_delay_seconds *before* constructing RosbagControl
(rosbag_control.load_rosbag_config), and Engine.sync_rosbag_config
persists new config arriving over the wire and applies start_mode/
start_delay_seconds live to an already-running RosbagControl.
"""
import json

from xparo.rosbag_control import DEFAULT_ROSBAG_CONFIG, ROSBAG_CONFIG_FILENAME, load_rosbag_config


class TestLoadRosbagConfig:
    def test_no_persisted_file_returns_the_default_config(self, tmp_path):
        assert load_rosbag_config(str(tmp_path)) == DEFAULT_ROSBAG_CONFIG

    def test_loads_whatever_was_persisted(self, tmp_path):
        (tmp_path / ROSBAG_CONFIG_FILENAME).write_text(json.dumps({
            "record_all": False, "ignore_topics": [], "include_topics": ["/joy"],
            "start_mode": "task", "start_delay_seconds": 0,
        }))

        config = load_rosbag_config(str(tmp_path))

        assert config["record_all"] is False
        assert config["include_topics"] == ["/joy"]
        assert config["start_mode"] == "task"

    def test_a_corrupt_file_falls_back_to_the_default_rather_than_raising(self, tmp_path):
        (tmp_path / ROSBAG_CONFIG_FILENAME).write_text("not json")
        assert load_rosbag_config(str(tmp_path)) == DEFAULT_ROSBAG_CONFIG

    def test_a_partial_file_is_filled_in_with_defaults(self, tmp_path):
        (tmp_path / ROSBAG_CONFIG_FILENAME).write_text(json.dumps({"start_delay_seconds": 15}))

        config = load_rosbag_config(str(tmp_path))

        assert config["start_delay_seconds"] == 15
        assert config["record_all"] is True  # filled in from DEFAULT_ROSBAG_CONFIG


def _make_engine(tmp_path, **kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    engine = Engine("secret", "proj-rosbag-config-sync-test", **kwargs)
    # Redirect off the real repo checkout, same reasoning as
    # test_plugin_loader.py's own _make_engine.
    engine.files["xparo_custom_behaviors_folder_path"] = str(tmp_path)
    return engine


class TestEngineSyncRosbagConfig:
    def test_persists_the_config_to_the_expected_file(self, tmp_path):
        engine = _make_engine(tmp_path)
        config = {
            "record_all": False, "ignore_topics": [], "include_topics": ["/odom"],
            "start_mode": "auto", "start_delay_seconds": 10,
        }

        engine.sync_rosbag_config(config)

        persisted = json.loads((tmp_path / ROSBAG_CONFIG_FILENAME).read_text())
        assert persisted == config
        # And it's readable back through the same loader xparo_ros.py uses.
        assert load_rosbag_config(str(tmp_path))["include_topics"] == ["/odom"]

    def test_with_no_bt_executor_is_a_safe_noop_beyond_persisting(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert engine.bt_executor is None  # default, matching RUN_TASK's own "no live node" posture

        engine.sync_rosbag_config({"start_mode": "task", "start_delay_seconds": 0})  # must not raise

    def test_applies_start_mode_and_delay_live_to_an_existing_rosbag_control(self, tmp_path):
        from unittest.mock import MagicMock

        engine = _make_engine(tmp_path)
        fake_rosbag_control = MagicMock(start_mode='auto', start_delay_seconds=0)
        fake_node = MagicMock(rosbag_control=fake_rosbag_control)
        engine.bt_executor = MagicMock(node=fake_node)

        engine.sync_rosbag_config({"start_mode": "task", "start_delay_seconds": 45})

        assert fake_rosbag_control.start_mode == "task"
        assert fake_rosbag_control.start_delay_seconds == 45

    def test_a_negative_delay_from_the_wire_is_clamped_to_zero(self, tmp_path):
        from unittest.mock import MagicMock

        engine = _make_engine(tmp_path)
        fake_rosbag_control = MagicMock(start_mode='auto', start_delay_seconds=10)
        fake_node = MagicMock(rosbag_control=fake_rosbag_control)
        engine.bt_executor = MagicMock(node=fake_node)

        engine.sync_rosbag_config({"start_mode": "auto", "start_delay_seconds": -5})

        assert fake_rosbag_control.start_delay_seconds == 0

    def test_not_currently_recording_bags_does_not_crash(self, tmp_path):
        """bt_executor.node.rosbag_control is None when record_bags wasn't
        set at launch -- same "safe no-op" as RUN_TASK's own guard."""
        from unittest.mock import MagicMock

        engine = _make_engine(tmp_path)
        fake_node = MagicMock(rosbag_control=None)
        engine.bt_executor = MagicMock(node=fake_node)

        engine.sync_rosbag_config({"start_mode": "auto", "start_delay_seconds": 0})  # must not raise


class TestOnWsMessageRosbagConfigSync:
    def test_rosbag_config_key_dispatches_to_sync_rosbag_config(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.transport.send = lambda message, command_for=None: None

        engine.on_ws_message('ws', {"rosbag_config": {
            "record_all": True, "ignore_topics": ["/rosout"], "include_topics": [],
            "start_mode": "auto", "start_delay_seconds": 5,
        }})

        persisted = json.loads((tmp_path / ROSBAG_CONFIG_FILENAME).read_text())
        assert persisted["ignore_topics"] == ["/rosout"]
        assert persisted["start_delay_seconds"] == 5
