"""Real-time ERROR/FATAL capture via /rosout, replacing the old
rosout.log file-tailer -- confirmed live (this session's own ~/.ros/log/
history) that rosout.log never actually exists in this ROS2 Jazzy setup,
only launch.log (the launch orchestrator's own messages, not node log
output), so the file-tailer could never have worked. /rosout is a real,
always-published topic every rclpy get_logger() call writes to.

Every captured line is one occurrence in the shared ProblemTracker -- a
repeat raises the count of one problem instead of being dropped (it used
to be reported only the first time per boot) or repeated as a new line.
"""
from unittest.mock import MagicMock

from rcl_interfaces.msg import Log

from xparo.health import ProblemTracker
from xparo.rosout_watcher import RosoutWatcher


class FakeNode:
    def __init__(self):
        self.subscriptions = []

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions.append((msg_type, topic, callback, qos))
        return MagicMock()


def _log(name, msg, level):
    return Log(name=name, msg=msg, level=level)


def _watcher():
    tracker = ProblemTracker()
    return RosoutWatcher(FakeNode(), tracker), tracker


def test_subscribes_to_the_real_rosout_topic():
    node = FakeNode()
    RosoutWatcher(node, ProblemTracker())
    assert len(node.subscriptions) == 1
    msg_type, topic, callback, qos = node.subscriptions[0]
    assert msg_type is Log
    assert topic == '/rosout'


def test_info_debug_and_warn_are_ignored():
    """"Error log" means actionable -- warnings come from /diagnostics."""
    watcher, tracker = _watcher()
    watcher._on_log(_log('n', 'just fyi', Log.INFO))
    watcher._on_log(_log('n', 'debug noise', Log.DEBUG))
    watcher._on_log(_log('n', 'a warning', Log.WARN))
    assert tracker.entries() == []


def test_error_and_fatal_are_captured_with_their_level():
    watcher, tracker = _watcher()
    watcher._on_log(_log('n', 'something broke', Log.ERROR))
    watcher._on_log(_log('n', 'total meltdown', Log.FATAL))
    levels = {e['message']: e['level'] for e in tracker.entries()}
    assert levels == {'something broke': 'error', 'total meltdown': 'fatal'}
    assert all(e['source'] == 'log' and e['name'] == 'n' for e in tracker.entries())


def test_a_repeating_error_is_one_problem_with_a_count():
    watcher, tracker = _watcher()
    for _ in range(5):
        watcher._on_log(_log('n', 'recurring problem', Log.ERROR))
    entries = tracker.entries()
    assert len(entries) == 1 and entries[0]['count'] == 5
    changes = tracker.flush()
    assert len(changes) == 1 and changes[0]['count_delta'] == 5


def test_the_same_message_from_a_different_logger_is_a_different_problem():
    watcher, tracker = _watcher()
    watcher._on_log(_log('node_a', 'same text', Log.ERROR))
    watcher._on_log(_log('node_b', 'same text', Log.ERROR))
    assert len(tracker.entries()) == 2
