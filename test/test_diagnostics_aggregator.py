"""ROS2 /diagnostics aggregation (see FLEET_OPS_GAPS.md's "critical" gap:
xparo's live status used to read only host CPU/RAM/disk, never the
standard /diagnostics topic). A fake node harness (mirrors
test_rosbag_control.py's own FakeNode/FakeClient convention) stands in
for a real rclpy.Node -- create_subscription only needs to record the
callback here so tests can invoke it directly with a real
DiagnosticArray/DiagnosticStatus message, not a mock.
"""
import time
from unittest.mock import MagicMock

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus

from xparo.diagnostics_aggregator import DiagnosticsAggregator, STALE_TIMEOUT_SEC


class FakeNode:
    def __init__(self):
        self.subscriptions = []

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions.append((msg_type, topic, callback, qos))
        return MagicMock()


def _status(name, level, message=''):
    return DiagnosticStatus(name=name, level=level, message=message)


class TestSubscription:
    def test_subscribes_to_the_standard_diagnostics_topic(self):
        node = FakeNode()
        DiagnosticsAggregator(node)
        assert len(node.subscriptions) == 1
        msg_type, topic, callback, qos = node.subscriptions[0]
        assert msg_type is DiagnosticArray
        assert topic == '/diagnostics'


class TestSnapshotFromRealDiagnosticMessages:
    def test_a_single_ok_status_reports_ok_overall(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        _, _, callback, _ = node.subscriptions[0]

        callback(DiagnosticArray(status=[_status('nav2/amcl', DiagnosticStatus.OK, 'localized')]))

        snapshot = aggregator.snapshot()
        assert snapshot["components"]["nav2/amcl"]["level"] == "ok"
        assert snapshot["components"]["nav2/amcl"]["message"] == "localized"
        assert snapshot["overall_level"] == "ok"

    def test_overall_level_is_the_worst_of_any_component(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        _, _, callback, _ = node.subscriptions[0]

        callback(DiagnosticArray(status=[
            _status('sensor/lidar', DiagnosticStatus.OK),
            _status('motor/left', DiagnosticStatus.WARN, 'temperature high'),
            _status('motor/right', DiagnosticStatus.ERROR, 'driver fault'),
        ]))

        snapshot = aggregator.snapshot()
        assert snapshot["overall_level"] == "error"
        assert snapshot["components"]["motor/right"]["level"] == "error"

    def test_a_later_message_for_the_same_name_replaces_the_earlier_one(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        _, _, callback, _ = node.subscriptions[0]

        callback(DiagnosticArray(status=[_status('battery', DiagnosticStatus.ERROR, 'low')]))
        callback(DiagnosticArray(status=[_status('battery', DiagnosticStatus.OK, 'charged')]))

        snapshot = aggregator.snapshot()
        assert snapshot["components"]["battery"]["level"] == "ok"
        assert snapshot["overall_level"] == "ok"

    def test_an_unrecognized_level_byte_is_treated_as_error_not_silently_dropped(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        _, _, callback, _ = node.subscriptions[0]
        callback(DiagnosticArray(status=[_status('weird', 99)]))
        assert aggregator.snapshot()["components"]["weird"]["level"] == "error"


class TestStaleness:
    def test_a_component_that_stops_reporting_becomes_stale_not_invisible(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        aggregator.record_self_status('xparo.disk_usage', 'ok', '10% used')
        aggregator._latest['xparo.disk_usage']['last_seen'] = time.time() - (STALE_TIMEOUT_SEC + 5)

        snapshot = aggregator.snapshot()

        assert snapshot["components"]["xparo.disk_usage"]["stale"] is True
        assert snapshot["components"]["xparo.disk_usage"]["level"] == "stale"
        assert snapshot["overall_level"] == "stale"

    def test_a_fresh_component_is_not_stale(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        aggregator.record_self_status('xparo.disk_usage', 'ok', '10% used')
        assert aggregator.snapshot()["components"]["xparo.disk_usage"]["stale"] is False


class TestRecordSelfStatus:
    def test_bypasses_the_diagnostics_topic_entirely(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        aggregator.record_self_status('xparo.rosbag_recorder', 'error', 'state=unknown')
        snapshot = aggregator.snapshot()
        assert snapshot["components"]["xparo.rosbag_recorder"] == {
            "level": "error", "message": "state=unknown", "stale": False,
        }
        assert snapshot["overall_level"] == "error"

    def test_no_entries_at_all_reports_ok_overall_not_an_error(self):
        node = FakeNode()
        aggregator = DiagnosticsAggregator(node)
        snapshot = aggregator.snapshot()
        assert snapshot == {"components": {}, "overall_level": "ok"}
