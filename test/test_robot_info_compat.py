"""What the robot info (ADD_robots_info) tells the dashboard about this
package, so the server can handle robots that are not updated yet (R4G):

- data["health"]["stale_keeps_error"]: health.py keeps a problem active when
  its component goes silent (STALE). Older packages marked it inactive; the
  server keeps a fallback for robots that don't send this.
- data["xparo_stage"]: the launch's stage. Only production counts as "in
  service" on the project's website; an older package sends no stage.
"""
import json
from unittest.mock import patch

import requests


def _make_database(tmp_path, **kwargs):
    from xparo.database import XP_Database
    return XP_Database(
        xparo_database_size=80,
        xparo_database_path=str(tmp_path),
        xparo_website_url='http://127.0.0.1:8765',
        BAG_DIR=str(tmp_path / 'bags'),
        record_bags=False,
        **kwargs,
    )


def _robot_info(database):
    sent = []
    with patch('xparo.database.requests.get', side_effect=requests.RequestException("offline")), \
         patch('xparo.database.detect_connectivity', return_value={}), \
         patch.object(database, 'get_best_ip', return_value=("10.0.0.5", "lan")), \
         patch.object(database, 'trigger_log_update'):
        database.send_robot_info(sent.append)
    return json.loads(sent[0])["ADD_robots_info"]


def test_robot_info_says_a_silent_component_keeps_its_error(tmp_path):
    info = _robot_info(_make_database(tmp_path))
    assert info["data"]["health"] == {"stale_keeps_error": True}


def test_the_flag_matches_what_health_py_does():
    """The flag must stay true only while health.py really keeps the error
    active when its component goes STALE (ROSAPI-20)."""
    from xparo import health
    tracker = health.ProblemTracker(clock=lambda: 1.0)
    tracker.record_diagnostic('lidar', 'error', 'no scan')
    tracker.record_diagnostic('lidar', 'stale', 'no status for over 30s')
    active = {(e['level'], e['message']): e['active'] for e in tracker.entries()}
    assert active[('error', 'no scan')] is True
    assert health.CAPABILITIES["stale_keeps_error"] is active[('error', 'no scan')]


def test_robot_info_sends_the_launch_stage(tmp_path):
    for stage in ("production", "review", "testing", "development"):
        info = _robot_info(_make_database(tmp_path, xparo_stage=stage))
        assert info["data"]["xparo_stage"] == stage


def test_no_stage_is_sent_when_none_is_known(tmp_path):
    info = _robot_info(_make_database(tmp_path))
    assert "xparo_stage" not in info["data"]


def test_the_engine_passes_its_stage_to_the_robot_info():
    from xparo.engine import Engine
    engine = Engine("secret", "proj-robot-info-test", connection_type="offline", xparo_stage="development")
    info = _robot_info(engine.local_database)
    assert info["data"]["xparo_stage"] == "development"
    assert info["data"]["health"] == {"stale_keeps_error": True}
