"""Real-time ERROR/FATAL capture, replacing the previous rosout.log
file-tailing approach entirely (confirmed live against this repo's own
~/.ros/log/ history: rosout.log genuinely never exists in this ROS2 Jazzy
setup -- only launch.log, which is just the launch orchestrator's own
messages, not node log output -- so the file-tailer was watching a file
that was never going to appear).

/rosout is the correct, standard fix: every rclpy get_logger() call
publishes an rcl_interfaces/msg/Log message there unconditionally,
regardless of whether anything is logging to a file at all. Subscribing
to it also means xparo's own internal errors (rosbag_control's "RECORDER
UNREACHABLE", a failed resync, etc.) get captured the same way any
external node's would -- something file-tailing could never have done
for THIS process's own stdout-only logging either.
"""
import time

from rcl_interfaces.msg import Log

ERROR_LEVEL_THRESHOLD = Log.ERROR  # WARN(30) is deliberately excluded -- "error log" means actionable, not noisy.
MAX_RECENT_ENTRIES = 200


def _level_name(level):
    return 'fatal' if level >= Log.FATAL else 'error'


class RosoutWatcher:
    """One instance per robot, owned by the Xparo rclpy Node (same pattern
    as RosbagControl/DiagnosticsAggregator) since it needs a real Node to
    subscribe on. Always subscribed from boot -- persistence (via
    on_new_error) doesn't depend on anyone having a popup open; only the
    *live push while watching* half does (see engine.py's WATCH_ERROR_LOGS).
    """

    def __init__(self, node, on_new_error=None):
        self.node = node
        # on_new_error(entry) -- called the instant a genuinely new (never
        # seen this boot) error/fatal message arrives, so engine.py can
        # push it to Django for persistent storage in real time, not on
        # some polling cadence.
        self.on_new_error = on_new_error
        self._recent = []  # bounded ring buffer, for a live-watch popup's own snapshot
        self._seen_signatures = set()  # (logger_name, message) already reported to Django this boot
        self.watch_enabled = False  # toggled by WATCH_ERROR_LOGS/UNWATCH_ERROR_LOGS
        self._live_push = None  # set by engine.py while a popup is actively watching
        self._subscription = node.create_subscription(Log, '/rosout', self._on_log, 10)

    def _on_log(self, msg):
        if msg.level < ERROR_LEVEL_THRESHOLD:
            return
        entry = {
            "logger_name": msg.name, "message": msg.msg,
            "level": _level_name(msg.level), "timestamp": time.time(),
        }
        self._recent.append(entry)
        if len(self._recent) > MAX_RECENT_ENTRIES:
            self._recent.pop(0)

        if self.watch_enabled and self._live_push is not None:
            self._live_push(entry)

        signature = (msg.name, msg.msg)
        if signature not in self._seen_signatures:
            self._seen_signatures.add(signature)
            if self.on_new_error is not None:
                self.on_new_error(entry)

    def recent_entries(self):
        return list(self._recent)
