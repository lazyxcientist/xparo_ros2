"""Real-time ERROR/FATAL capture from /rosout, replacing the previous
rosout.log file-tailing approach entirely (confirmed live against this
repo's own ~/.ros/log/ history: rosout.log genuinely never exists in this
ROS2 Jazzy setup -- only launch.log, which is just the launch
orchestrator's own messages, not node log output -- so the file-tailer was
watching a file that was never going to appear).

/rosout is the correct, standard fix: every rclpy get_logger() call
publishes an rcl_interfaces/msg/Log message there unconditionally,
regardless of whether anything is logging to a file at all. Subscribing
to it also means xparo's own internal errors get captured the same way
any external node's would.

Every line is one occurrence in the shared health.ProblemTracker, so a
repeated error shows once with a growing count -- it used to be reported
to the dashboard only the first time per boot (count stuck at 1 until a
restart) while "Watch Live" printed every repeat as a new line.
"""
from rcl_interfaces.msg import Log

ERROR_LEVEL_THRESHOLD = Log.ERROR  # WARN(30) is deliberately excluded -- "error log" means actionable, not noisy.


def _level_name(level):
    return 'fatal' if level >= Log.FATAL else 'error'


class RosoutWatcher:
    """One instance per robot, owned by the Xparo rclpy Node (same pattern
    as RosbagControl/DiagnosticsAggregator) since it needs a real Node to
    subscribe on."""

    def __init__(self, node, tracker):
        self.node = node
        self.tracker = tracker
        self._subscription = node.create_subscription(Log, '/rosout', self._on_log, 50)

    def _on_log(self, msg):
        if msg.level < ERROR_LEVEL_THRESHOLD:
            return
        self.tracker.record_log(msg.name, _level_name(msg.level), msg.msg)
