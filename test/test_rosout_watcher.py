"""Real-time ERROR/FATAL capture via /rosout, replacing the old
rosout.log file-tailer -- confirmed live (this session's own ~/.ros/log/
history) that rosout.log never actually exists in this ROS2 Jazzy setup,
only launch.log (the launch orchestrator's own messages, not node log
output), so the file-tailer could never have worked. /rosout is a real,
always-published topic every rclpy get_logger() call writes to.
"""
from unittest.mock import MagicMock

from rcl_interfaces.msg import Log

from xparo.rosout_watcher import RosoutWatcher, ERROR_LEVEL_THRESHOLD, MAX_RECENT_ENTRIES


class FakeNode:
    def __init__(self):
        self.subscriptions = []

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions.append((msg_type, topic, callback, qos))
        return MagicMock()


def _log(name, msg, level):
    return Log(name=name, msg=msg, level=level)


class TestSubscription:
    def test_subscribes_to_the_real_rosout_topic(self):
        node = FakeNode()
        RosoutWatcher(node)
        assert len(node.subscriptions) == 1
        msg_type, topic, callback, qos = node.subscriptions[0]
        assert msg_type is Log
        assert topic == '/rosout'


class TestLevelFiltering:
    def test_info_and_debug_are_ignored(self):
        node = FakeNode()
        watcher = RosoutWatcher(node)
        watcher._on_log(_log('n', 'just fyi', Log.INFO))
        watcher._on_log(_log('n', 'debug noise', Log.DEBUG))
        assert watcher.recent_entries() == []

    def test_warn_is_deliberately_excluded_too(self):
        """"Error log" means actionable, not noisy -- matches the old
        file-tailer's own ERROR/FATAL-only scope."""
        node = FakeNode()
        watcher = RosoutWatcher(node)
        watcher._on_log(_log('n', 'a warning', Log.WARN))
        assert watcher.recent_entries() == []

    def test_error_and_fatal_are_captured(self):
        node = FakeNode()
        watcher = RosoutWatcher(node)
        watcher._on_log(_log('n', 'something broke', Log.ERROR))
        watcher._on_log(_log('n', 'total meltdown', Log.FATAL))
        entries = watcher.recent_entries()
        assert len(entries) == 2
        assert entries[0]["level"] == "error"
        assert entries[1]["level"] == "fatal"
        assert entries[0]["logger_name"] == "n"
        assert entries[0]["message"] == "something broke"
        assert "timestamp" in entries[0]


class TestRecentEntriesBuffer:
    def test_bounded_to_max_recent_entries(self):
        node = FakeNode()
        watcher = RosoutWatcher(node)
        for i in range(MAX_RECENT_ENTRIES + 10):
            watcher._on_log(_log('n', f'error {i}', Log.ERROR))
        entries = watcher.recent_entries()
        assert len(entries) == MAX_RECENT_ENTRIES
        # Oldest entries were dropped -- the tail survives.
        assert entries[-1]["message"] == f"error {MAX_RECENT_ENTRIES + 9}"

    def test_recent_entries_returns_a_copy_not_the_live_buffer(self):
        node = FakeNode()
        watcher = RosoutWatcher(node)
        watcher._on_log(_log('n', 'e1', Log.ERROR))
        snapshot = watcher.recent_entries()
        watcher._on_log(_log('n', 'e2', Log.ERROR))
        assert len(snapshot) == 1  # unaffected by the later append


class TestOnNewErrorDedup:
    def test_a_genuinely_new_error_fires_on_new_error_once(self):
        seen = []
        node = FakeNode()
        watcher = RosoutWatcher(node, on_new_error=seen.append)
        watcher._on_log(_log('n', 'first time', Log.ERROR))
        assert len(seen) == 1
        assert seen[0]["message"] == "first time"

    def test_the_exact_same_signature_repeating_does_not_refire_on_new_error(self):
        """Don't store/notify duplicate, repetitive errors -- de-dup keyed
        on (logger_name, message), matching engine.py's own module
        docstring reasoning for RosoutWatcher."""
        seen = []
        node = FakeNode()
        watcher = RosoutWatcher(node, on_new_error=seen.append)
        for _ in range(5):
            watcher._on_log(_log('n', 'recurring problem', Log.ERROR))
        assert len(seen) == 1
        # But it's still captured in the live buffer every time, for the
        # live-watch popup's own view of what's actually happening.
        assert len(watcher.recent_entries()) == 5

    def test_the_same_message_from_a_different_logger_is_a_different_signature(self):
        seen = []
        node = FakeNode()
        watcher = RosoutWatcher(node, on_new_error=seen.append)
        watcher._on_log(_log('node_a', 'same text', Log.ERROR))
        watcher._on_log(_log('node_b', 'same text', Log.ERROR))
        assert len(seen) == 2

    def test_no_callback_configured_does_not_raise(self):
        node = FakeNode()
        watcher = RosoutWatcher(node)  # on_new_error=None
        watcher._on_log(_log('n', 'x', Log.ERROR))  # must not raise


class TestLiveWatchPush:
    def test_disabled_by_default_no_push_even_with_a_push_callback_set(self):
        pushed = []
        node = FakeNode()
        watcher = RosoutWatcher(node)
        watcher._live_push = pushed.append
        watcher._on_log(_log('n', 'x', Log.ERROR))
        assert pushed == []

    def test_enabled_pushes_every_captured_entry_live_not_just_new_ones(self):
        """Real-time, event-driven -- no polling delay, and (unlike
        on_new_error) fires for every repeat too, since a live popup
        watching right now should see everything happening, not just
        first occurrences."""
        pushed = []
        node = FakeNode()
        watcher = RosoutWatcher(node)
        watcher._live_push = pushed.append
        watcher.watch_enabled = True
        watcher._on_log(_log('n', 'first', Log.ERROR))
        watcher._on_log(_log('n', 'first', Log.ERROR))
        assert len(pushed) == 2

    def test_disabling_stops_further_live_pushes(self):
        pushed = []
        node = FakeNode()
        watcher = RosoutWatcher(node)
        watcher._live_push = pushed.append
        watcher.watch_enabled = True
        watcher._on_log(_log('n', 'e1', Log.ERROR))
        watcher.watch_enabled = False
        watcher._on_log(_log('n', 'e2', Log.ERROR))
        assert len(pushed) == 1
