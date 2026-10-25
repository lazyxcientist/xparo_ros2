"""Breakage review #2: a Wi-Fi drop mid-drive left the robot on its last
stick command, and delayed TCP bursts replayed old sticks later."""
from xparo.remote_ops import TeleopDeadman, handle_teleop


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _deadman():
    published, clock = [], Clock()
    d = TeleopDeadman(lambda a, b: published.append((a, b)), timeout_s=0.5, clock=clock)
    d._ensure_thread = lambda: None  # tests drive check() themselves
    return d, published, clock


def test_robot_stops_when_commands_stop_arriving():
    d, published, clock = _deadman()
    d.command([1, 0.5], [1])
    clock.t += 0.3
    assert d.check() is False                # still within the window
    clock.t += 0.3
    assert d.check() is True                 # 0.6 s of silence: stop
    assert published[-1] == ([0.0] * 4, [0] * 3)
    clock.t += 5
    assert d.check() is False                # stops once, not every tick


def test_a_steady_stream_keeps_driving():
    d, published, clock = _deadman()
    for _ in range(20):
        d.command([1, 0, 0, 0], [0, 0, 0])
        clock.t += 0.08
        assert d.check() is False
    assert all(axes[0] == 1.0 for axes, _ in published)


def test_centred_stick_needs_no_extra_stop():
    d, published, clock = _deadman()
    d.command([0, 0, 0, 0], [0, 0, 0])
    clock.t += 2
    assert d.check() is False
    assert len(published) == 1


def test_delayed_out_of_order_commands_are_dropped():
    d, published, _ = _deadman()
    assert d.command([0.2], [], seq=5, session="tab1") is True
    assert d.command([0.9], [], seq=3, session="tab1") is False   # arrived late
    assert d.command([0.9], [], seq=5, session="tab1") is False   # duplicate
    assert d.command([0.3], [], seq=6, session="tab1") is True
    assert d.command([0.4], [], seq=1, session="tab2") is True    # new tab starts over
    assert [a[0] for a, _ in published] == [0.2, 0.3, 0.4]


def test_disabled_deadman_never_stops():
    published = []
    d = TeleopDeadman(lambda a, b: published.append(a), timeout_s=0, clock=lambda: 0)
    d.command([1], [])
    assert d.check() is False


def test_handle_teleop_goes_through_the_deadman_and_acks_stale():
    d, published, _ = _deadman()
    acks = []
    handle_teleop([1], [], None, acks.append, deadman=d, seq=2, session="s")
    handle_teleop([1], [], None, acks.append, deadman=d, seq=1, session="s")
    assert acks == [{"TELEOP_ACK": {"success": True, "stale": False}},
                    {"TELEOP_ACK": {"success": True, "stale": True}}]
    assert len(published) == 1


def test_nan_and_huge_values_are_still_sanitised():
    d, published, _ = _deadman()
    d.command([float("nan"), 99, -99, "x"], [5])
    assert published[0] == ([0.0, 1.0, -1.0, 0.0], [1, 0, 0])
