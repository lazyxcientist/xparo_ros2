"""Covers blackbox_manager.py's BlackboxOrchestrator after Phase 3's rewire:
start_recording()/stop_recording() now delegate to RosbagControl instead of
subprocess.Popen("ros2 bag record", ...), and _process_uploads()'s "is a
session currently open" check reads RosbagControl.state instead of a
Popen object's truthiness.
"""
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from xparo.blackbox_manager import BlackboxOrchestrator
from xparo.rosbag_control import CLOSED, WRITING


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
