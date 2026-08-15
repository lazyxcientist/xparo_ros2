"""_rosbag_record_topic_args (launch/xparo_launch.py) -- builds the
topic-selection portion of `ros2 bag record`'s command line from whatever
recording config was last synced from the dashboard. Topic selection is a
process launch argument, so this can only ever reflect this robot's
*next* launch -- see the function's own docstring.

Loaded via importlib against the file's explicit path (like
plugin_loader.py's own _load_module_from_file) rather than a normal
`import launch.xparo_launch` -- `launch/` here is a plain data directory
sitting next to the `xparo` Python package (src/xparo/launch/), not a
proper importable submodule of it, and its name collides with the real
`launch` ROS2 package this same file imports from at its own top level.
"""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCH_FILE = os.path.normpath(os.path.join(HERE, '..', 'launch', 'xparo_launch.py'))


def _load_xparo_launch_module():
    spec = importlib.util.spec_from_file_location('xparo_launch_under_test', LAUNCH_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def xparo_launch():
    return _load_xparo_launch_module()


class TestRosbagRecordTopicArgs:
    def test_no_persisted_config_defaults_to_recording_everything(self, xparo_launch, tmp_path):
        assert xparo_launch._rosbag_record_topic_args(str(tmp_path)) == '-a'

    def test_record_all_with_no_ignored_topics_is_plain_dash_a(self, xparo_launch, tmp_path):
        (tmp_path / 'rosbag_config.json').write_text(json.dumps({
            'record_all': True, 'ignore_topics': [], 'include_topics': [],
            'start_mode': 'auto', 'start_delay_seconds': 0,
        }))
        assert xparo_launch._rosbag_record_topic_args(str(tmp_path)) == '-a'

    def test_record_all_with_ignored_topics_adds_an_anchored_exclude_regex(self, xparo_launch, tmp_path):
        (tmp_path / 'rosbag_config.json').write_text(json.dumps({
            'record_all': True, 'ignore_topics': ['/rosout', '/tf'], 'include_topics': [],
            'start_mode': 'auto', 'start_delay_seconds': 0,
        }))

        result = xparo_launch._rosbag_record_topic_args(str(tmp_path))

        assert result.startswith('-a --exclude ')
        assert '^/rosout$' in result
        assert '^/tf$' in result
        # Anchored -- must not also match /tf_static.
        assert '/tf_static' not in result

    def test_not_record_all_uses_the_explicit_include_list(self, xparo_launch, tmp_path):
        (tmp_path / 'rosbag_config.json').write_text(json.dumps({
            'record_all': False, 'ignore_topics': [], 'include_topics': ['/joy', '/odom'],
            'start_mode': 'auto', 'start_delay_seconds': 0,
        }))

        assert xparo_launch._rosbag_record_topic_args(str(tmp_path)) == '/joy /odom'

    def test_not_record_all_with_nothing_selected_falls_back_to_recording_everything(self, xparo_launch, tmp_path):
        """An empty include list would otherwise silently write a bag with
        nothing in it forever -- indistinguishable from "recording is
        broken" on the Sensors Database page."""
        (tmp_path / 'rosbag_config.json').write_text(json.dumps({
            'record_all': False, 'ignore_topics': [], 'include_topics': [],
            'start_mode': 'auto', 'start_delay_seconds': 0,
        }))

        assert xparo_launch._rosbag_record_topic_args(str(tmp_path)) == '-a'
