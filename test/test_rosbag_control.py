"""Covers rosbag_control.py's state machine -- ported from tethered_module's
rosbag_node.py, this is the highest-stakes logic in Phase 3 (a wrong state
determination means either silently not recording, or silently recording
into the wrong session). Uses a minimal fake rclpy Node/Client/Future
harness rather than a real ROS2 graph -- add_done_callback is invoked
synchronously (unlike real rclpy, which runs it on the executor), so the
full resync()/handle_start()/handle_stop() callback chains execute inline
and are directly assertable without spinning anything.
"""
from unittest.mock import MagicMock

import pytest

from xparo.rosbag_control import RosbagControl, CLOSED, PAUSED, WRITING, UNKNOWN


class FakeFuture:
    def __init__(self, result=None, exception=None):
        self._result = result
        self._exception = exception

    def result(self):
        if self._exception:
            raise self._exception
        return self._result

    def add_done_callback(self, cb):
        cb(self)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.responses = []  # list of FakeFuture, consumed in order
        self.service_available = True

    def queue_response(self, *futures):
        self.responses.extend(futures)
        return self

    def wait_for_service(self, timeout_sec=None):
        return self.service_available

    def call_async(self, request):
        self.calls.append(request)
        if self.responses:
            return self.responses.pop(0)
        return FakeFuture(result=MagicMock())


class FakeNode:
    def __init__(self):
        self.clients = {}
        self.timers = []

    def create_client(self, srv_type, name):
        client = FakeClient()
        self.clients[name] = client
        return client

    def create_subscription(self, *args, **kwargs):
        return MagicMock()

    def create_publisher(self, *args, **kwargs):
        return MagicMock()

    def create_timer(self, period, callback):
        timer = MagicMock()
        self.timers.append((period, callback, timer))
        return timer

    def get_logger(self):
        return MagicMock()


def _discovery_response(running):
    return FakeFuture(result=MagicMock(running=running))


def _paused_response(paused):
    return FakeFuture(result=MagicMock(paused=paused))


def _make_control(bag_dir='/tmp/bags'):
    """Constructs against a discovery-not-running response so __init__'s
    resync()->handle_stop() boot sequence resolves to a clean CLOSED
    baseline with no further calls (the common, simplest case) -- tests
    that care about a different starting state reconfigure fake clients
    and call resync()/handle_start()/handle_stop() again explicitly.
    """
    node = FakeNode()
    control = RosbagControl.__new__(RosbagControl)
    control.node = node
    control.bag_dir = bag_dir
    control.state = UNKNOWN
    control.recorder_alive = False
    from rosbag2_interfaces.srv import Record, Stop, Resume, SplitBagfile, IsPaused, IsDiscoveryRunning
    control.record_client = node.create_client(Record, '/rosbag2_recorder/record')
    control.stop_client = node.create_client(Stop, '/rosbag2_recorder/stop')
    control.resume_client = node.create_client(Resume, '/rosbag2_recorder/resume')
    control.split_client = node.create_client(SplitBagfile, '/rosbag2_recorder/split_bagfile')
    control.is_paused_client = node.create_client(IsPaused, '/rosbag2_recorder/is_paused')
    control.is_discovery_client = node.create_client(IsDiscoveryRunning, '/rosbag2_recorder/is_discovery_running')
    control.status_pub = MagicMock()
    control.alive_pub = MagicMock()
    control.is_discovery_client.queue_response(_discovery_response(False))
    control.resync(on_done=control.handle_stop)
    return control


def test_resync_closed_when_discovery_not_running():
    control = _make_control()
    assert control.state == CLOSED
    # Boot-time handle_stop() must not have issued a real Stop call --
    # skip-if-already-closed is the whole point.
    assert control.stop_client.calls == []


def test_resync_writing_when_discovery_running_and_not_paused():
    control = _make_control()
    control.is_discovery_client.queue_response(_discovery_response(True))
    control.is_paused_client.queue_response(_paused_response(False))
    control.resync()
    assert control.state == WRITING


def test_resync_paused_when_discovery_running_and_paused():
    control = _make_control()
    control.is_discovery_client.queue_response(_discovery_response(True))
    control.is_paused_client.queue_response(_paused_response(True))
    control.resync()
    assert control.state == PAUSED


def test_resync_unknown_when_discovery_service_unreachable():
    control = _make_control()
    control.is_discovery_client.service_available = False
    control.resync()
    assert control.state == UNKNOWN
    assert control.recorder_alive is False


def test_boot_sequence_stops_a_session_left_open_at_launch():
    """The empirically-discovered fix: ros2 bag record always auto-opens a
    session at its own process launch (confirmed against a real Jazzy
    recorder) -- landing PAUSED, not CLOSED, even with --start-paused. If
    __init__ didn't close this out, the first real handle_start() would
    silently just Resume it (writing into the launch-time throwaway path)
    instead of opening a fresh, correctly-timestamped session.
    """
    node = FakeNode()
    from rosbag2_interfaces.srv import Record, Stop, Resume, SplitBagfile, IsPaused, IsDiscoveryRunning
    control = RosbagControl.__new__(RosbagControl)
    control.node = node
    control.bag_dir = '/tmp/bags'
    control.state = UNKNOWN
    control.recorder_alive = False
    control.record_client = node.create_client(Record, '/rosbag2_recorder/record')
    control.stop_client = node.create_client(Stop, '/rosbag2_recorder/stop')
    control.resume_client = node.create_client(Resume, '/rosbag2_recorder/resume')
    control.split_client = node.create_client(SplitBagfile, '/rosbag2_recorder/split_bagfile')
    control.is_paused_client = node.create_client(IsPaused, '/rosbag2_recorder/is_paused')
    control.is_discovery_client = node.create_client(IsDiscoveryRunning, '/rosbag2_recorder/is_discovery_running')
    control.status_pub = MagicMock()
    control.alive_pub = MagicMock()

    # Simulate the real recorder's boot state: session open, paused.
    control.is_discovery_client.queue_response(_discovery_response(True))
    control.is_paused_client.queue_response(_paused_response(True))
    control.resync(on_done=control.handle_stop)

    assert len(control.stop_client.calls) == 1


def test_handle_start_from_closed_opens_a_new_timestamped_session():
    control = _make_control()
    assert control.state == CLOSED
    control.record_client.queue_response(FakeFuture(result=MagicMock(return_code=0, error_string='')))
    control.handle_start()

    assert len(control.record_client.calls) == 1
    uri = control.record_client.calls[0].uri
    assert uri.startswith('/tmp/bags/bag_')
    # Successful Record -> Resume, per handle_start's on_record_response.
    assert len(control.resume_client.calls) == 1


def test_handle_start_from_paused_resumes_without_opening_a_new_session():
    control = _make_control()
    control.is_discovery_client.queue_response(_discovery_response(True))
    control.is_paused_client.queue_response(_paused_response(True))
    control.resync()
    assert control.state == PAUSED

    control.handle_start()
    assert control.record_client.calls == []
    assert len(control.resume_client.calls) == 1


def test_handle_start_from_writing_is_a_noop():
    control = _make_control()
    control.is_discovery_client.queue_response(_discovery_response(True))
    control.is_paused_client.queue_response(_paused_response(False))
    control.resync()
    assert control.state == WRITING

    control.handle_start()
    assert control.record_client.calls == []
    assert control.resume_client.calls == []


def test_handle_start_from_unknown_retries_itself_after_resync():
    """A real gap this closes: start_recording() can be called (via
    BlackboxOrchestrator, from XP_Database.__init__) before the host
    node's executor has ever spun, i.e. before resync() has resolved
    anything -- handle_start() must not just give up in that case.
    """
    control = _make_control()
    control.state = UNKNOWN
    # First resync() call (triggered by handle_start) resolves CLOSED,
    # which should then retry handle_start() -> Record.
    control.is_discovery_client.queue_response(_discovery_response(False))
    control.record_client.queue_response(FakeFuture(result=MagicMock(return_code=0, error_string='')))

    control.handle_start()

    assert control.state == CLOSED  # resolved by the retried resync path
    assert len(control.record_client.calls) == 1
    assert len(control.resume_client.calls) == 1


def test_handle_start_record_rejected_does_not_call_resume():
    control = _make_control()
    assert control.state == CLOSED
    control.record_client.queue_response(
        FakeFuture(result=MagicMock(return_code=1, error_string='disk full'))
    )
    # resync() runs again after rejection -- keep it resolving CLOSED.
    control.is_discovery_client.queue_response(_discovery_response(False))

    control.handle_start()

    assert control.resume_client.calls == []


def test_handle_stop_from_closed_is_a_noop():
    control = _make_control()
    assert control.state == CLOSED
    control.handle_stop()
    assert control.stop_client.calls == []


def test_handle_stop_from_writing_calls_stop():
    control = _make_control()
    control.is_discovery_client.queue_response(_discovery_response(True))
    control.is_paused_client.queue_response(_paused_response(False))
    control.resync()
    assert control.state == WRITING

    control.handle_stop()
    assert len(control.stop_client.calls) == 1


def test_split_disabled_via_control_cb_never_reaches_split_client():
    control = _make_control()
    msg = MagicMock(data='split')
    control.control_cb(msg)
    assert control.split_client.calls == []
