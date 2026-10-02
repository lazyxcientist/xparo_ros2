"""health.py: the one deduplicated, counted problem list (fed by
/diagnostics and /rosout) and xparo's own /diagnostics statuses."""
from xparo import health
from xparo.health import ProblemTracker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _tracker():
    clock = Clock()
    return ProblemTracker(clock=clock), clock


def test_a_diagnostics_error_republished_every_second_counts_once():
    tracker, clock = _tracker()
    for _ in range(30):
        tracker.record_diagnostic('lidar', 'error', 'no scan data')
        clock.now += 1
    entries = tracker.entries()
    assert len(entries) == 1
    assert entries[0]['count'] == 1 and entries[0]['active'] is True
    assert entries[0]['last_seen'] == clock.now - 1


def test_going_ok_resolves_and_coming_back_counts_again():
    tracker, clock = _tracker()
    tracker.record_diagnostic('lidar', 'error', 'no scan data')
    tracker.record_diagnostic('lidar', 'ok', 'fine')
    assert tracker.entries()[0]['active'] is False
    tracker.record_diagnostic('lidar', 'error', 'no scan data')
    entry = tracker.entries()[0]
    assert entry['count'] == 2 and entry['active'] is True


def test_a_new_message_for_the_same_component_is_a_separate_problem():
    tracker, _ = _tracker()
    tracker.record_diagnostic('motor', 'warn', 'hot: 70C')
    tracker.record_diagnostic('motor', 'error', 'overheated: 90C')
    by_message = {e['message']: e for e in tracker.entries()}
    assert by_message['hot: 70C']['active'] is False       # superseded
    assert by_message['overheated: 90C']['active'] is True


def test_ok_statuses_never_become_problems():
    tracker, _ = _tracker()
    tracker.record_diagnostic('battery', 'ok', '87%')
    assert tracker.entries() == [] and tracker.flush() == []


def test_flush_reports_only_changes_with_deltas():
    tracker, _ = _tracker()
    tracker.record_log('nav2', 'error', 'costmap failed')
    tracker.record_log('nav2', 'error', 'costmap failed')
    first = tracker.flush()
    assert [(c['name'], c['count'], c['count_delta']) for c in first] == [('nav2', 2, 2)]
    assert tracker.flush() == []  # nothing new
    tracker.record_log('nav2', 'error', 'costmap failed')
    assert tracker.flush()[0]['count_delta'] == 1


def test_resolving_is_reported_even_without_new_occurrences():
    tracker, _ = _tracker()
    tracker.record_diagnostic('lidar', 'error', 'no scan data')
    tracker.flush()
    tracker.record_diagnostic('lidar', 'ok', '')
    changes = tracker.flush()
    assert len(changes) == 1 and changes[0]['active'] is False and changes[0]['count_delta'] == 0


def test_old_inactive_entries_are_evicted_but_active_ones_kept():
    clock = Clock()
    tracker = ProblemTracker(clock=clock, max_entries=3)
    tracker.record_diagnostic('keep', 'error', 'still broken')
    for i in range(5):
        clock.now += 1
        tracker.record_log('n', 'error', f'e{i}')
    names = [e['message'] for e in tracker.entries()]
    assert len(names) == 3 and 'still broken' in names


def test_self_statuses_no_recorder_is_ok_unless_this_launch_started_one():
    statuses = dict((n, (lvl, msg)) for n, lvl, msg in health.self_statuses(
        {"mode": "websocket", "connected": True},
        {"alive": False, "state": "unknown", "owns_launch_process": False},
        {"running": [], "last": None}, 40.0))
    assert statuses['xparo: rosbag recorder'][0] == 'ok'
    assert statuses['xparo: dashboard connection'] == ('ok', 'connected')
    assert statuses['xparo: task engine'] == ('ok', 'idle')
    assert statuses['xparo: disk usage'][0] == 'ok'

    owned = dict((n, lvl) for n, lvl, _ in health.self_statuses(
        {"mode": "websocket", "connected": False},
        {"alive": False, "state": "unknown", "owns_launch_process": True},
        {"running": [], "last": None}, 96.0))
    assert owned == {'xparo: dashboard connection': 'error', 'xparo: rosbag recorder': 'error',
                     'xparo: task engine': 'ok', 'xparo: disk usage': 'error'}


def test_self_statuses_task_engine_reports_a_failed_task_until_the_next_one():
    failed = dict((n, (lvl, msg)) for n, lvl, msg in health.self_statuses(
        {"mode": "websocket", "connected": True}, None,
        {"running": [], "last": {"title": "Deliver", "success": False, "explanation": "<hello> isn't a node"}}, None))
    assert failed['xparo: task engine'][0] == 'warn'
    assert 'Deliver' in failed['xparo: task engine'][1] and "<hello>" in failed['xparo: task engine'][1]
    running = dict((n, (lvl, msg)) for n, lvl, msg in health.self_statuses(
        {"mode": "websocket", "connected": True}, None,
        {"running": ["Deliver"], "last": {"title": "Deliver", "success": False}}, None))
    assert running['xparo: task engine'] == ('ok', 'running: Deliver')


def test_a_full_flush_restates_every_active_problem():
    tracker, _ = _tracker()
    tracker.record_diagnostic('lidar', 'error', 'no scan data')
    tracker.record_log('nav2', 'error', 'x')
    tracker.flush()
    assert tracker.flush() == []
    full = tracker.flush(full=True)
    assert [(c['name'], c['count_delta'], c['active']) for c in full] == [('lidar', 0, True)]
