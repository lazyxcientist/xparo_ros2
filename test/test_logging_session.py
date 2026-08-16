"""XP_Database's logging session lifecycle: one Logs_history row per real
process session, not one per transport reconnect (send_robot_info's
session_id guard), plus honest session-start/session-end/whole-session
average CPU/RAM/disk/GPU consumption (trigger_log_update/
update_logging_session/stop_logging_session's own docstrings have the full
context -- this was a real bug found live: a robot that reconnected a few
times over one otherwise-continuous run got a brand new Logs_history row
every single time).
"""
import subprocess
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _make_database(tmp_path):
    from xparo.database import XP_Database
    return XP_Database(
        xparo_database_size=80,
        xparo_database_path=str(tmp_path),
        xparo_website_url='http://127.0.0.1:8000',
        BAG_DIR=str(tmp_path / 'bags'),
        record_bags=False,
    )


def _sent_keys(mock_send):
    import json
    return [json.loads(call.args[0]) for call in mock_send.call_args_list]


class TestSendRobotInfoSessionGuard:
    def test_first_connect_starts_a_session(self, tmp_path):
        db = _make_database(tmp_path)
        sent = MagicMock()
        assert db.session_id is None

        with patch.object(db, 'trigger_log_update') as mock_trigger:
            db.send_robot_info(sent)

        mock_trigger.assert_called_once()

    def test_a_reconnect_with_an_already_active_session_does_not_start_a_second_one(self, tmp_path):
        """The actual bug: send_initial_data (this method's only caller)
        is the transport's on_connected callback, firing on every
        reconnect -- not just true process startup."""
        db = _make_database(tmp_path)
        db.session_id = "already-active-session"
        sent = MagicMock()

        with patch.object(db, 'trigger_log_update') as mock_trigger:
            db.send_robot_info(sent)

        mock_trigger.assert_not_called()

    def test_a_session_that_never_confirmed_retries_on_the_next_reconnect(self, tmp_path):
        """session_id is only set once Django's "log_updated" reply
        actually lands (dashboard_receive) -- if that never happened
        (dropped connection, etc.), session_id is still None, and the
        next reconnect correctly retries rather than losing the session."""
        db = _make_database(tmp_path)
        assert db.session_id is None
        sent = MagicMock()

        with patch.object(db, 'trigger_log_update') as mock_trigger:
            db.send_robot_info(sent)
            db.send_robot_info(sent)  # still None -- log_updated never arrived

        assert mock_trigger.call_count == 2


class TestGetGpuPercent:
    def test_returns_none_when_nvidia_smi_is_not_installed(self, tmp_path):
        """Real, unmocked call -- this sandbox genuinely has no nvidia-smi,
        so this exercises the actual FileNotFoundError path for real."""
        db = _make_database(tmp_path)
        assert db.get_gpu_percent() is None

    def test_parses_a_real_looking_nvidia_smi_reply(self, tmp_path):
        db = _make_database(tmp_path)
        with patch('xparo.database.subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="42\n", stderr="",
            )
            assert db.get_gpu_percent() == 42.0

    def test_a_nonzero_exit_code_is_treated_as_no_gpu(self, tmp_path):
        db = _make_database(tmp_path)
        with patch('xparo.database.subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="no devices",
            )
            assert db.get_gpu_percent() is None

    def test_malformed_output_does_not_raise(self, tmp_path):
        db = _make_database(tmp_path)
        with patch('xparo.database.subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not-a-number\n", stderr="",
            )
            assert db.get_gpu_percent() is None


class TestAverageResourceSamples:
    def test_no_samples_returns_an_empty_dict(self, tmp_path):
        db = _make_database(tmp_path)
        assert db._average_resource_samples() == {}

    def test_averages_cpu_ram_disk_across_every_sample(self, tmp_path):
        db = _make_database(tmp_path)
        db.resource_samples = [
            (datetime.utcnow(), {"cpu_avg_percent": 10, "ram_used_percent": 40, "disk_used_percent": 50, "gpu_percent": None}),
            (datetime.utcnow(), {"cpu_avg_percent": 30, "ram_used_percent": 60, "disk_used_percent": 70, "gpu_percent": None}),
        ]

        averages = db._average_resource_samples()

        assert averages["avg_cpu_percent"] == 20
        assert averages["avg_ram_percent"] == 50
        assert averages["avg_disk_percent"] == 60
        assert averages["sample_count"] == 2

    def test_gpu_average_is_omitted_entirely_when_never_seen(self, tmp_path):
        """'No GPU seen' and 'GPU idle at 0%' are different facts --
        omitted, not defaulted to 0."""
        db = _make_database(tmp_path)
        db.resource_samples = [
            (datetime.utcnow(), {"cpu_avg_percent": 10, "ram_used_percent": 40, "disk_used_percent": 50, "gpu_percent": None}),
        ]

        averages = db._average_resource_samples()

        assert "avg_gpu_percent" not in averages

    def test_gpu_average_only_folds_in_samples_where_a_gpu_was_actually_detected(self, tmp_path):
        db = _make_database(tmp_path)
        db.resource_samples = [
            (datetime.utcnow(), {"cpu_avg_percent": 10, "ram_used_percent": 40, "disk_used_percent": 50, "gpu_percent": None}),
            (datetime.utcnow(), {"cpu_avg_percent": 10, "ram_used_percent": 40, "disk_used_percent": 50, "gpu_percent": 20}),
            (datetime.utcnow(), {"cpu_avg_percent": 10, "ram_used_percent": 40, "disk_used_percent": 50, "gpu_percent": 40}),
        ]

        averages = db._average_resource_samples()

        assert averages["avg_gpu_percent"] == 30  # (20+40)/2, not /3


class TestUpdateLoggingSession:
    def test_no_active_session_is_a_safe_noop(self, tmp_path):
        db = _make_database(tmp_path)
        sent = MagicMock()
        db.update_logging_session(sent)
        sent.assert_not_called()

    def test_sends_an_update_with_a_running_average_and_appends_a_sample(self, tmp_path):
        db = _make_database(tmp_path)
        db.session_id = "session-1"
        sent = MagicMock()

        with patch.object(db, 'get_smart_resource_consumption', return_value={
            "cpu_avg_percent": 15, "ram_used_percent": 45, "disk_used_percent": 55, "gpu_percent": None,
        }), patch.object(db, 'get_ros2_jazzy_logs', return_value={"log_directory": "x", "new_entries": []}):
            db.update_logging_session(sent)

        assert len(db.resource_samples) == 1
        payload = _sent_keys(sent)[0]["UPDATE_Logs_history_database"]
        assert payload["id"] == "session-1"
        assert payload["extra_info"]["running_average"]["avg_cpu_percent"] == 15


class TestStopLoggingSession:
    def test_no_active_session_is_a_safe_noop(self, tmp_path):
        db = _make_database(tmp_path)
        sent = MagicMock()
        db.stop_logging_session(sent)
        sent.assert_not_called()

    def test_sends_total_runtime_and_averages_then_clears_the_session(self, tmp_path):
        db = _make_database(tmp_path)
        db.session_id = "session-1"
        db.session_start_time = datetime.utcnow() - timedelta(seconds=120)
        db.resource_samples = [
            (datetime.utcnow(), {"cpu_avg_percent": 20, "ram_used_percent": 40, "disk_used_percent": 60, "gpu_percent": None}),
        ]
        sent = MagicMock()

        db.stop_logging_session(sent)

        payload = _sent_keys(sent)[0]["UPDATE_Logs_history_database"]
        assert payload["id"] == "session-1"
        assert payload["extra_info"]["event"] == "session_end"
        assert payload["extra_info"]["total_runtime_seconds"] >= 120
        assert payload["extra_info"]["avg_cpu_percent"] == 20
        assert db.session_id is None
        assert db.resource_samples == []


class TestTriggerLogUpdate:
    def test_sends_a_session_start_event_with_a_timestamp(self, tmp_path):
        db = _make_database(tmp_path)
        sent = MagicMock()

        with patch.object(db, 'get_smart_resource_consumption', return_value={}), \
             patch.object(db, 'get_ros2_jazzy_logs', return_value={"log_directory": "x", "new_entries": []}):
            db.trigger_log_update(sent)

        payload = _sent_keys(sent)[0]["ADD_Logs_history_database"]
        assert payload["extra_info"]["event"] == "session_start"
        assert "session_start_time" in payload["extra_info"]
