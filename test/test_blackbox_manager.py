"""Covers blackbox_manager.py's BlackboxOrchestrator after Phase 3's rewire:
start_recording()/stop_recording() now delegate to RosbagControl instead of
subprocess.Popen("ros2 bag record", ...), and _process_uploads()'s "is a
session currently open" check reads RosbagControl.state instead of a
Popen object's truthiness.
"""
import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from xparo.blackbox_manager import BlackboxOrchestrator
from xparo.rosbag_control import CLOSED, WRITING

# None of the tests below are about metadata.yaml content -- they predate
# rosbag_metadata.py entirely and use fixture bag files with no sibling
# metadata.yaml. Without this patch, build_sensor_data() would shell out to
# a real `ros2 bag reindex` subprocess for every one of them (slow, and a
# hard dependency on ROS2 being on PATH in whatever environment runs these
# tests) just to conclude, correctly, "no metadata available". Patched at
# module scope here since it's the correct behavior for every test in this
# file except the dedicated TestUploadSendsRealMetadata class below, which
# overrides it per-test.
pytestmark = pytest.mark.usefixtures('_no_real_reindex_subprocess')


@pytest.fixture
def _no_real_reindex_subprocess():
    with patch('xparo.rosbag_metadata.try_reindex', return_value=False):
        yield


def _make_orchestrator(tmp_path, rosbag_control=None):
    return BlackboxOrchestrator(
        ROBOT_ID='robot-1',
        xparo_website_url='http://127.0.0.1:8000',
        BAG_DIR=str(tmp_path),
        rosbag_control=rosbag_control,
    )


def test_start_recording_delegates_to_rosbag_control(tmp_path):
    rc = MagicMock()
    orchestrator = _make_orchestrator(tmp_path, rosbag_control=rc)
    orchestrator.start_recording()
    rc.handle_start.assert_called_once()


def test_stop_recording_delegates_to_rosbag_control(tmp_path):
    rc = MagicMock()
    orchestrator = _make_orchestrator(tmp_path, rosbag_control=rc)
    orchestrator.stop_recording()
    rc.handle_stop.assert_called_once()


def test_start_and_stop_recording_are_safe_noops_without_rosbag_control(tmp_path):
    orchestrator = _make_orchestrator(tmp_path, rosbag_control=None)
    # Must not raise -- e.g. Engine constructed standalone/under test,
    # outside a live rclpy node (xparo_ros.py).
    orchestrator.start_recording()
    orchestrator.stop_recording()


def test_api_token_defaults_to_empty_string_not_a_hardcoded_secret(tmp_path):
    """Was a hardcoded, seemingly-real-looking token literal -- defaulting
    to "" instead matches manage_disk_and_upload's own gate ("Idle: Waiting
    for API_TOKEN from server..." only prints when the token IS empty), and
    means the correct per-deployment token (delivered via engine.py's
    REST_API_TOKEN handler) can never race against a stale hardcoded one.
    """
    orchestrator = _make_orchestrator(tmp_path)
    assert orchestrator.API_TOKEN == ""


class _FakeRosbagControl:
    def __init__(self, state):
        self.state = state


def _write_two_bags_oldest_first(tmp_path):
    older = tmp_path / 'older.mcap'
    newer = tmp_path / 'newer.mcap'
    older.write_bytes(b'data')
    newer.write_bytes(b'data')
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    return older, newer


def test_process_uploads_excludes_newest_file_while_session_open(tmp_path):
    older, newer = _write_two_bags_oldest_first(tmp_path)
    orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(WRITING))
    orchestrator.API_TOKEN = "tok"

    with patch('xparo.blackbox_manager.requests.post') as mock_post:
        mock_post.return_value = MagicMock(status_code=201)
        orchestrator._process_uploads()

    mock_post.assert_called_once()
    assert not older.exists()  # uploaded and deleted
    assert newer.exists()      # still-open session's active file, left alone


def test_process_uploads_includes_all_files_when_session_closed(tmp_path):
    older, newer = _write_two_bags_oldest_first(tmp_path)
    orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
    orchestrator.API_TOKEN = "tok"

    with patch('xparo.blackbox_manager.requests.post') as mock_post:
        mock_post.return_value = MagicMock(status_code=201)
        orchestrator._process_uploads()

    assert mock_post.call_count == 2
    assert not older.exists()
    assert not newer.exists()


def test_process_uploads_includes_all_files_when_no_rosbag_control(tmp_path):
    older, newer = _write_two_bags_oldest_first(tmp_path)
    orchestrator = _make_orchestrator(tmp_path, rosbag_control=None)
    orchestrator.API_TOKEN = "tok"

    with patch('xparo.blackbox_manager.requests.post') as mock_post:
        mock_post.return_value = MagicMock(status_code=201)
        orchestrator._process_uploads()

    assert mock_post.call_count == 2
    assert not older.exists()
    assert not newer.exists()


class TestFindBagFilesMatchesRealCompressedOutput:
    """Regression coverage for a real bug found live: xparo_launch.py's
    ExecuteProcess always passes --compression-mode file --compression-
    format zstd, so a real session's output is actually named
    *.mcap.zstd -- the original glob ("**/*.mcap" only) matched zero real
    files, ever, completely silently. 7 fully-closed, fully-compressed
    bag sessions sat unclaimed on disk across hours of testing before
    this was caught, because nothing here ever printed so much as a
    warning about it. Every existing test above used plain "*.mcap"
    filenames, which is exactly why they never caught this -- the bug
    only manifests for the real, compressed filename shape.
    """

    def test_a_compressed_zstd_bag_is_found_and_uploaded(self, tmp_path):
        bag_dir = tmp_path / 'bag_2026_08_15-18_44_35'
        bag_dir.mkdir()
        bag_file = bag_dir / 'bag_2026_08_15-18_44_35_0.mcap.zstd'
        bag_file.write_bytes(b'compressed data')
        (bag_dir / 'metadata.yaml').write_text('not a bag')  # must not be picked up as one

        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            orchestrator._process_uploads()

        mock_post.assert_called_once()
        assert not bag_file.exists()

    def test_find_bag_files_matches_both_plain_and_compressed_extensions(self, tmp_path):
        (tmp_path / 'a.mcap').write_bytes(b'x')
        (tmp_path / 'b.mcap.zstd').write_bytes(b'x')
        orchestrator = _make_orchestrator(tmp_path)

        found = {os.path.basename(f) for f in orchestrator._find_bag_files()}

        assert found == {'a.mcap', 'b.mcap.zstd'}

    def test_a_session_still_open_excludes_the_compressed_file_too(self, tmp_path):
        """The still-open session's *active* file is plain .mcap (it only
        becomes *.mcap.zstd once the recorder finalizes and compresses it
        on stop) -- this exercises the exclusion still working correctly
        once a second, real .mcap.zstd file is also on disk."""
        older_zstd = tmp_path / 'older' / 'older_0.mcap.zstd'
        older_zstd.parent.mkdir()
        older_zstd.write_bytes(b'x')
        newer_open = tmp_path / 'newer' / 'newer_0.mcap'
        newer_open.parent.mkdir()
        newer_open.write_bytes(b'x')
        now = time.time()
        os.utime(older_zstd, (now - 100, now - 100))
        os.utime(newer_open, (now, now))

        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(WRITING))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            orchestrator._process_uploads()

        mock_post.assert_called_once()
        assert not older_zstd.exists()
        assert newer_open.exists()


class TestUploadSendsRealMetadata:
    """Regression coverage for a second real bug found live, alongside the
    *.mcap vs *.mcap.zstd one above: even once a bag file was correctly
    found and uploaded, the payload's 'data' field was hardcoded to the
    literal string '{}' -- metadata.yaml (written by the recorder on a
    clean Stop(), and confirmed present on disk for every completed real
    session) was never read at all. That's why the Sensors Database page's
    "Data (preview)" column was always empty regardless of how a recording
    ended. build_sensor_data() (rosbag_metadata.py) fixes that; these tests
    cover the wiring into _process_uploads() specifically -- rosbag_metadata
    module's own logic (summarizing, reindex recovery, malformed YAML) is
    covered directly in test_rosbag_metadata.py.
    """

    def _bag_with_metadata(self, tmp_path, yaml_text):
        bag_dir = tmp_path / 'session'
        bag_dir.mkdir()
        (bag_dir / 'metadata.yaml').write_text(yaml_text)
        bag_file = bag_dir / 'session_0.mcap.zstd'
        bag_file.write_bytes(b'compressed bag bytes')
        return bag_dir, bag_file

    def test_uploaded_payload_carries_the_real_parsed_metadata(self, tmp_path):
        yaml_text = (
            "rosbag2_bagfile_information:\n"
            "  message_count: 7\n"
            "  duration:\n"
            "    nanoseconds: 2000000000\n"
            "  topics_with_message_count:\n"
            "    - topic_metadata:\n"
            "        name: /odom\n"
            "        type: nav_msgs/msg/Odometry\n"
            "      message_count: 7\n"
        )
        bag_dir, bag_file = self._bag_with_metadata(tmp_path, yaml_text)
        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            orchestrator._process_uploads()

        sent_data = json.loads(mock_post.call_args.kwargs['data']['data'])
        assert sent_data['metadata_status'] == 'ok'
        assert sent_data['summary']['message_count'] == 7
        assert sent_data['summary']['topics'][0]['name'] == '/odom'
        assert 'rosbag2_bagfile_information' in sent_data['raw_yaml']

    def test_metadata_yaml_is_deleted_alongside_the_bag_on_success(self, tmp_path):
        bag_dir, bag_file = self._bag_with_metadata(tmp_path, "rosbag2_bagfile_information: {}\n")
        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            orchestrator._process_uploads()

        # Previously only the bag data file was removed -- metadata.yaml
        # (and therefore the whole session directory) was orphaned forever,
        # since os.listdir(parent) was never empty.
        assert not bag_file.exists()
        assert not (bag_dir / 'metadata.yaml').exists()
        assert not bag_dir.exists()

    def test_metadata_yaml_is_left_alone_when_the_upload_fails(self, tmp_path):
        bag_dir, bag_file = self._bag_with_metadata(tmp_path, "rosbag2_bagfile_information: {}\n")
        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text='server error')
            orchestrator._process_uploads()

        assert bag_file.exists()
        assert (bag_dir / 'metadata.yaml').exists()

    def test_force_cleanup_also_removes_the_sibling_metadata_yaml(self, tmp_path):
        bag_dir, bag_file = self._bag_with_metadata(tmp_path, "rosbag2_bagfile_information: {}\n")
        orchestrator = _make_orchestrator(tmp_path)

        # Above DISK_TARGET_PCT (70%) so the loop doesn't break before
        # deleting this one and only file.
        with patch.object(orchestrator, 'get_disk_usage', return_value=95.0):
            orchestrator._force_cleanup()

        assert not bag_file.exists()
        assert not (bag_dir / 'metadata.yaml').exists()
        assert not bag_dir.exists()

    def test_no_metadata_yaml_at_all_still_uploads_with_an_unavailable_status(self, tmp_path):
        """The genuine force-exit case (recorder killed before Stop() could
        run) -- covered end-to-end here rather than just in
        test_rosbag_metadata.py, to confirm the bag itself still gets
        uploaded (it may still hold real, playable data) instead of being
        silently skipped just because metadata is missing."""
        bag_dir = tmp_path / 'orphan'
        bag_dir.mkdir()
        bag_file = bag_dir / 'orphan_0.mcap.zstd'
        bag_file.write_bytes(b'compressed bag bytes, but no metadata.yaml ever got written')
        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            orchestrator._process_uploads()

        mock_post.assert_called_once()
        sent_data = json.loads(mock_post.call_args.kwargs['data']['data'])
        assert sent_data == {'metadata_status': 'unavailable'}
        assert not bag_file.exists()


class TestDiscardBootSessionBags:
    """Regression coverage for a third real bug found live, in the same
    family as the *.mcap-vs-*.mcap.zstd and metadata-hardcoded-to-'{}'
    ones above: xparo_launch.py's ExecuteProcess can't start the recorder
    fully idle, so *every* `ros2 launch` also produces a second, throwaway
    'boot_session_<timestamp>' session at boot, immediately closed again
    by RosbagControl's own cleanup before it ever opens the real session a
    user actually wants (see rosbag_control.py's __init__ docstring).
    Before this fix, that throwaway got uploaded right alongside the real
    recording -- a single robot session always produced two rosbag rows
    on the dashboard instead of one.
    """

    def _finalized_bag(self, tmp_path, dirname, filename):
        bag_dir = tmp_path / dirname
        bag_dir.mkdir()
        (bag_dir / filename).write_bytes(b'compressed bag bytes')
        (bag_dir / 'metadata.yaml').write_text('rosbag2_bagfile_information: {}\n')
        return bag_dir, bag_dir / filename

    def test_a_finalized_boot_session_bag_is_deleted_not_uploaded(self, tmp_path):
        boot_dir, boot_file = self._finalized_bag(tmp_path, 'boot_session_20260816_010000', 'boot_session_20260816_010000_0.mcap.zstd')
        orchestrator = _make_orchestrator(tmp_path)

        orchestrator._discard_boot_session_bags()

        assert not boot_file.exists()
        assert not (boot_dir / 'metadata.yaml').exists()
        assert not boot_dir.exists()

    def test_an_unfinalized_boot_session_bag_is_left_alone_this_cycle(self, tmp_path):
        """No metadata.yaml yet -- RosbagControl's own async boot cleanup
        (a chain of ROS service calls) hasn't actually closed this
        session yet. Must not delete a file the recorder might still have
        open, or race its own compression step."""
        bag_dir = tmp_path / 'boot_session_20260816_010000'
        bag_dir.mkdir()
        bag_file = bag_dir / 'boot_session_20260816_010000_0.mcap'
        bag_file.write_bytes(b'still being written')
        orchestrator = _make_orchestrator(tmp_path)

        orchestrator._discard_boot_session_bags()

        assert bag_file.exists()

    def test_a_real_session_is_never_touched_regardless_of_metadata_presence(self, tmp_path):
        real_dir, real_file = self._finalized_bag(tmp_path, 'bag_2026_08_16-01_00_00', 'bag_2026_08_16-01_00_00_0.mcap.zstd')
        orchestrator = _make_orchestrator(tmp_path)

        orchestrator._discard_boot_session_bags()

        assert real_file.exists()

    def test_process_uploads_never_uploads_a_boot_session_bag_even_run_alone(self, tmp_path):
        """_process_uploads must be correct on its own, not merely because
        production's real loop always calls _discard_boot_session_bags
        first -- a caller that (like this test) invokes it directly must
        never see a boot-session throwaway treated as real data."""
        self._finalized_bag(tmp_path, 'boot_session_20260816_010000', 'boot_session_20260816_010000_0.mcap.zstd')
        real_dir, real_file = self._finalized_bag(tmp_path, 'bag_2026_08_16-01_05_00', 'bag_2026_08_16-01_05_00_0.mcap.zstd')
        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            orchestrator._process_uploads()

        mock_post.assert_called_once()
        uploaded_filename = mock_post.call_args.kwargs['files']['bag_file'].name
        assert 'boot_session' not in uploaded_filename
        assert not real_file.exists()  # the real session WAS uploaded and cleaned up

    def test_a_full_cycle_uploads_exactly_one_bag_per_real_session(self, tmp_path):
        """End-to-end: one real session plus its boot-cleanup twin on
        disk, run through the exact sequence manage_disk_and_upload's real
        loop uses (_discard_boot_session_bags then _process_uploads) --
        exactly one upload happens, not two."""
        self._finalized_bag(tmp_path, 'boot_session_20260816_010000', 'boot_session_20260816_010000_0.mcap.zstd')
        self._finalized_bag(tmp_path, 'bag_2026_08_16-01_05_00', 'bag_2026_08_16-01_05_00_0.mcap.zstd')
        orchestrator = _make_orchestrator(tmp_path, rosbag_control=_FakeRosbagControl(CLOSED))
        orchestrator.API_TOKEN = "tok"

        with patch('xparo.blackbox_manager.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            orchestrator._discard_boot_session_bags()
            orchestrator._process_uploads()

        assert mock_post.call_count == 1
