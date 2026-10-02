"""ROS2 /diagnostics aggregation -- xparo's own answer to the ROS2 community's
own stated best practice (see FLEET_OPS_GAPS.md's "critical" gap: xparo's
live status used to read only host CPU/RAM/disk, never the standard
/diagnostics topic every sensor driver, motor controller, and nav2 node
already publishes to, categorized OK/WARN/ERROR/STALE).

Deliberately NOT a full port of ros/diagnostic_aggregator (no analyzer
groups, no bond-based liveness) -- a much smaller, honest subset:
subscribe to /diagnostics, keep the latest DiagnosticStatus per name, and
derive one overall level. Real hardware may publish nothing to
/diagnostics at all (confirmed: nothing in this repo does today), so
record_self_status lets xparo also report a handful of things it already
knows about itself -- see engine.py's _refresh_self_diagnostics -- so the
feature is meaningful immediately, not an empty list on a fresh robot.
"""
import time

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus

STALE_TIMEOUT_SEC = 30.0

_LEVEL_NAMES = {
    DiagnosticStatus.OK: 'ok',
    DiagnosticStatus.WARN: 'warn',
    DiagnosticStatus.ERROR: 'error',
    DiagnosticStatus.STALE: 'stale',
}
_LEVEL_RANK = {'ok': 0, 'warn': 1, 'error': 2, 'stale': 3}


class DiagnosticsAggregator:
    """One instance per robot, owned by the Xparo rclpy Node (same
    ownership pattern as RosbagControl -- see xparo_ros.py) since it needs
    a real Node to subscribe on. `_latest` intentionally never expires an
    entry on its own; snapshot() computes staleness fresh every call
    instead, so a component that stops publishing shows up as STALE
    (not silently disappears) until something publishes for it again.
    """

    def __init__(self, node, tracker=None):
        self.node = node
        # health.ProblemTracker -- gets every status so problems are
        # counted/deduplicated for the Health & Errors popup.
        self.tracker = tracker
        self._latest = {}  # name -> {"level": str, "message": str, "last_seen": float}
        self._subscription = node.create_subscription(
            DiagnosticArray, '/diagnostics', self._on_diagnostics, 10,
        )

    def _on_diagnostics(self, msg):
        now = time.time()
        for status in msg.status:
            level = _LEVEL_NAMES.get(status.level, 'error')
            self._latest[status.name] = {
                "level": level,
                "message": status.message,
                "last_seen": now,
                "hardware_id": status.hardware_id,
            }
            if self.tracker is not None:
                self.tracker.record_diagnostic(status.name, level, status.message, status.hardware_id)

    def record_self_status(self, name, level, message=''):
        """xparo's own self-reported entries, for when nothing publishes
        them on /diagnostics (Engine outside a live node) -- same storage/
        staleness handling either way."""
        self._latest[name] = {"level": level, "message": message, "last_seen": time.time()}
        if self.tracker is not None:
            self.tracker.record_diagnostic(name, level, message)

    def check_stale(self):
        """A component that stopped publishing is a problem too -- counted
        once when it goes quiet (called periodically by xparo_ros.py)."""
        if self.tracker is None:
            return
        now = time.time()
        for name, entry in self._latest.items():
            if (now - entry["last_seen"]) > STALE_TIMEOUT_SEC:
                self.tracker.record_diagnostic(
                    name, 'stale', f"no status for over {STALE_TIMEOUT_SEC:.0f}s", entry.get("hardware_id", ''))

    def snapshot(self):
        """{"components": {name: {level, message, stale}}, "overall_level": str}."""
        now = time.time()
        components = {}
        overall = 'ok'
        for name, entry in self._latest.items():
            stale = (now - entry["last_seen"]) > STALE_TIMEOUT_SEC
            level = 'stale' if stale else entry["level"]
            components[name] = {"level": level, "message": entry["message"], "stale": stale,
                                 "last_seen": entry["last_seen"]}
            if _LEVEL_RANK[level] > _LEVEL_RANK[overall]:
                overall = level
        return {"components": components, "overall_level": overall}
