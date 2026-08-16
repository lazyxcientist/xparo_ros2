"""rosbag_metadata.py -- reads a completed session's metadata.yaml into
what blackbox_manager.py now uploads instead of a hardcoded 'data': '{}'
(the reason the Sensors Database page's "Data (preview)" was always empty).
"""
import os
import subprocess

import pytest

from xparo.rosbag_metadata import build_sensor_data, summarize_metadata, try_reindex

REAL_METADATA_YAML = """\
rosbag2_bagfile_information:
  version: 9
  storage_identifier: mcap
  duration:
    nanoseconds: 5000000000
  starting_time:
    nanoseconds_since_epoch: 1786800000000000000
  message_count: 30
  topics_with_message_count:
    - topic_metadata:
        name: /odom
        type: nav_msgs/msg/Odometry
        serialization_format: cdr
        offered_qos_profiles:
          - history: unknown
            depth: 0
            reliability: reliable
            durability: volatile
            deadline:
              sec: 9223372036
              nsec: 854775807
        type_description_hash: RIHS01_abc
      message_count: 5
    - topic_metadata:
        name: /rosout
        type: rcl_interfaces/msg/Log
        serialization_format: cdr
        offered_qos_profiles: []
        type_description_hash: RIHS01_def
      message_count: 25
  compression_format: zstd
  compression_mode: FILE
  relative_file_paths:
    - test_bag_0.mcap.zstd
  files:
    - path: test_bag_0.mcap
      starting_time:
        nanoseconds_since_epoch: 1786800000000000000
      duration:
        nanoseconds: 5000000000
      message_count: 30
  custom_data: ~
  ros_distro: jazzy
"""


def _bag_dir_with_metadata(tmp_path, yaml_text=REAL_METADATA_YAML):
    bag_dir = tmp_path / "test_bag"
    bag_dir.mkdir()
    (bag_dir / "metadata.yaml").write_text(yaml_text)
    bag_file = bag_dir / "test_bag_0.mcap.zstd"
    bag_file.write_bytes(b"fake compressed bag bytes")
    return str(bag_file)


class TestSummarizeMetadata:
    def test_strips_qos_noise_and_keeps_the_useful_fields(self):
        import yaml
        parsed = yaml.safe_load(REAL_METADATA_YAML)
        summary = summarize_metadata(parsed)

        assert summary["duration_seconds"] == 5.0
        assert summary["message_count"] == 30
        assert summary["topic_count"] == 2
        assert summary["compression_format"] == "zstd"
        assert summary["ros_distro"] == "jazzy"
        # No QoS/type_description_hash noise leaked into the topic entries.
        assert set(summary["topics"][0].keys()) == {"name", "type", "message_count"}

    def test_topics_are_sorted_busiest_first(self):
        import yaml
        parsed = yaml.safe_load(REAL_METADATA_YAML)
        summary = summarize_metadata(parsed)

        assert [t["name"] for t in summary["topics"]] == ["/rosout", "/odom"]

    def test_missing_sections_do_not_raise(self):
        assert summarize_metadata({}) == {
            "storage_identifier": "", "ros_distro": "", "compression_format": "",
            "compression_mode": "", "duration_seconds": 0.0, "message_count": 0,
            "topic_count": 0, "starting_time_unix": None, "topics": [],
        }
        assert summarize_metadata(None)["topic_count"] == 0


class TestBuildSensorData:
    def test_reads_a_real_metadata_yaml_successfully(self, tmp_path):
        bag_file = _bag_dir_with_metadata(tmp_path)
        result = build_sensor_data(bag_file)

        assert result["metadata_status"] == "ok"
        assert result["summary"]["message_count"] == 30
        assert "rosbag2_bagfile_information" in result["raw_yaml"]

    def test_no_metadata_and_reindex_cannot_help_reports_unavailable(self, tmp_path):
        bag_dir = tmp_path / "orphan_bag"
        bag_dir.mkdir()
        bag_file = bag_dir / "orphan_bag_0.mcap"
        bag_file.write_bytes(b"")  # SIGKILL-truncated, exactly like a real force-exit

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(subprocess, "run", MagicMockThatNeverCreatesAFile())
            result = build_sensor_data(str(bag_file))

        assert result == {"metadata_status": "unavailable"}

    def test_reindex_recovering_metadata_is_reported_as_reindexed(self, tmp_path):
        bag_dir = tmp_path / "recovered_bag"
        bag_dir.mkdir()
        bag_file = bag_dir / "recovered_bag_0.mcap"
        bag_file.write_bytes(b"some real chunk data")

        def fake_run(cmd, **kwargs):
            # Simulates `ros2 bag reindex` succeeding at producing a file.
            (bag_dir / "metadata.yaml").write_text(REAL_METADATA_YAML)
            return subprocess.CompletedProcess(cmd, 0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(subprocess, "run", fake_run)
            result = build_sensor_data(str(bag_file))

        assert result["metadata_status"] == "reindexed"
        assert result["summary"]["message_count"] == 30

    def test_unparseable_yaml_reports_unparseable_not_a_crash(self, tmp_path):
        bag_file = _bag_dir_with_metadata(tmp_path, yaml_text="not: valid: yaml: [")
        result = build_sensor_data(bag_file)
        assert result == {"metadata_status": "unparseable"}


class TestTryReindex:
    def test_returns_true_immediately_if_metadata_already_exists(self, tmp_path):
        bag_dir = tmp_path / "already_has_metadata"
        bag_dir.mkdir()
        (bag_dir / "metadata.yaml").write_text(REAL_METADATA_YAML)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(subprocess, "run", MagicMockThatNeverCreatesAFile())
            assert try_reindex(str(bag_dir)) is True

    def test_a_missing_ros2_binary_never_raises(self, tmp_path):
        bag_dir = tmp_path / "no_ros2_on_path"
        bag_dir.mkdir()

        def raise_not_found(cmd, **kwargs):
            raise FileNotFoundError("ros2 not found")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(subprocess, "run", raise_not_found)
            assert try_reindex(str(bag_dir)) is False


class MagicMockThatNeverCreatesAFile:
    """A tiny subprocess.run stand-in that does nothing -- simulates a real
    `ros2 bag reindex` invocation on an unrecoverable (e.g. 0-byte,
    SIGKILL-truncated) bag file, which exits without producing metadata.yaml."""

    def __call__(self, cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1)
