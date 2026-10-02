"""Robot health for the dashboard's Health & Errors popup.

ProblemTracker is the one place problems are deduplicated and counted.
Fed by:
  - /diagnostics (DiagnosticsAggregator): every node's DiagnosticStatus,
    including xparo's own (self_statuses below, published on /diagnostics
    by xparo_ros.py so standard ROS tools see them too). A component
    counts one occurrence each time it ENTERS a warn/error/stale state (or
    its problem message changes) -- not once per 1 Hz republish of the
    same state, which would make the count meaningless.
  - /rosout (RosoutWatcher): ERROR/FATAL log lines from any node, one
    occurrence per line.

The same problem never becomes a second row; its count goes up instead.
Changes are batched (flush) so a node spamming the same error many times
a second costs one small message every few seconds, not one per line.
No ROS imports: fully testable without a ROS graph.
"""
import hashlib
import time

PROBLEM_LEVELS = ('warn', 'error', 'stale', 'fatal')
_RANK = {'ok': 0, 'warn': 1, 'stale': 2, 'error': 3, 'fatal': 4}
MAX_ENTRIES = 300
MAX_MESSAGE_CHARS = 2000


def problem_id(source, name, level, message):
    raw = f"{source}\x1f{name}\x1f{level}\x1f{message}".encode('utf-8', 'replace')
    return hashlib.sha1(raw).hexdigest()[:16]


class ProblemTracker:
    def __init__(self, clock=time.time, max_entries=MAX_ENTRIES):
        self._clock = clock
        self._max_entries = max_entries
        self._entries = {}        # id -> entry dict
        self._pending = {}        # id -> occurrences not yet flushed
        self._dirty = set()       # ids changed since the last flush (count or active flag)
        self._component = {}      # diagnostics name -> (level, message) last seen

    # ---- inputs --------------------------------------------------------
    def record_log(self, logger_name, level, message):
        """One /rosout line = one occurrence."""
        self._occurrence('log', logger_name, level, message, active=None)

    def record_diagnostic(self, name, level, message='', hardware_id=''):
        """Latest status of one /diagnostics component (called on every
        republish). Only a change INTO a problem state counts."""
        message = (message or '').strip()
        previous = self._component.get(name)
        self._component[name] = (level, message)
        if level not in PROBLEM_LEVELS:
            # Back to OK: whatever this component was complaining about is
            # resolved -- kept in the list (with its count), no longer active.
            self._set_component_inactive(name)
            return
        entry_id = problem_id('diagnostics', name, level, message)
        if previous == (level, message) and entry_id in self._entries:
            entry = self._entries[entry_id]
            entry['last_seen'] = self._clock()
            if not entry['active']:
                entry['active'] = True
                self._dirty.add(entry_id)
            return
        # A different problem for the same component replaces the old one
        # as "the active one".
        self._set_component_inactive(name)
        self._occurrence('diagnostics', name, level, message, active=True, hardware_id=hardware_id)

    # ---- outputs -------------------------------------------------------
    def flush(self, full=False):
        """Changes since the last flush: list of entry dicts, each with
        count_delta (new occurrences since last flush). Empty if nothing
        changed. full=True also includes every currently active problem
        (count_delta 0 if unchanged) -- sent first after each connect, so
        the dashboard can resolve whatever an earlier process of this
        robot left marked as ongoing."""
        ids = set(self._dirty)
        if full:
            ids |= {entry_id for entry_id, entry in self._entries.items() if entry['active']}
        changes = []
        for entry_id in ids:
            entry = self._entries.get(entry_id)
            if entry is None:
                continue
            changes.append({**entry, 'count_delta': self._pending.get(entry_id, 0)})
        self._dirty.clear()
        self._pending.clear()
        changes.sort(key=lambda e: e['last_seen'])
        return changes

    def entries(self):
        return sorted(self._entries.values(), key=lambda e: (-_RANK.get(e['level'], 0), -e['last_seen']))

    # ---- internals -----------------------------------------------------
    def _occurrence(self, source, name, level, message, active, hardware_id=''):
        message = (message or '')[:MAX_MESSAGE_CHARS]
        now = self._clock()
        entry_id = problem_id(source, name, level, message)
        entry = self._entries.get(entry_id)
        if entry is None:
            entry = {
                'id': entry_id, 'source': source, 'name': name, 'level': level, 'message': message,
                'hardware_id': hardware_id or '', 'count': 0, 'first_seen': now, 'last_seen': now,
                'active': active,
            }
            self._entries[entry_id] = entry
            self._evict()
        entry['count'] += 1
        entry['last_seen'] = now
        if active is not None:
            entry['active'] = active
        self._pending[entry_id] = self._pending.get(entry_id, 0) + 1
        self._dirty.add(entry_id)

    def _set_component_inactive(self, name):
        for entry_id, entry in self._entries.items():
            if entry['source'] == 'diagnostics' and entry['name'] == name and entry['active']:
                entry['active'] = False
                self._dirty.add(entry_id)

    def _evict(self):
        if len(self._entries) <= self._max_entries:
            return
        # Oldest inactive first; never an active diagnostics problem.
        candidates = sorted(
            (e for e in self._entries.values() if not e['active']), key=lambda e: e['last_seen'])
        for entry in candidates[:len(self._entries) - self._max_entries]:
            self._entries.pop(entry['id'], None)
            self._pending.pop(entry['id'], None)
            self._dirty.discard(entry['id'])


# ----------------------------------------------------------------------
# xparo's own statuses, published on /diagnostics
# ----------------------------------------------------------------------
SELF_PREFIX = 'xparo: '


def self_statuses(connection, rosbag, task, disk_percent):
    """[(name, level, message)] for xparo itself.

    connection: {"mode": "websocket"|"rest"|"hybrid"|"offline"|"tethered_tcp", "connected": bool}
    rosbag:     None (no RosbagControl) or {"alive", "state", "owns_launch_process"}
    task:       {"running": [titles], "last": None or {"title", "success", "explanation"}}
    disk_percent: float or None
    """
    statuses = []

    mode = connection.get('mode')
    if mode == 'offline':
        statuses.append(('dashboard connection', 'ok', 'offline mode -- not connecting to the dashboard'))
    elif connection.get('connected'):
        statuses.append(('dashboard connection', 'ok', 'connected' if mode != 'rest' else 'connected (REST polling)'))
    else:
        statuses.append(('dashboard connection', 'error', 'not connected to the dashboard -- retrying'))

    if rosbag is not None:
        if rosbag.get('alive'):
            label = {'writing': 'recording', 'paused': 'session open, paused', 'closed': 'not recording'}
            statuses.append(('rosbag recorder', 'ok', label.get(rosbag.get('state'), f"state {rosbag.get('state')}")))
        elif rosbag.get('owns_launch_process'):
            statuses.append(('rosbag recorder', 'error',
                             'recorder unreachable -- launched with record_bags:=true but its services are gone'))
        else:
            statuses.append(('rosbag recorder', 'ok', 'no recorder running (recording off)'))

    running = task.get('running') or []
    last = task.get('last')
    if running:
        statuses.append(('task engine', 'ok', 'running: ' + ', '.join(running)))
    elif last and not last.get('success'):
        why = last.get('explanation') or 'failed'
        statuses.append(('task engine', 'warn', f"last task {last.get('title') or ''!s} failed: {why}".replace('  ', ' ')))
    else:
        statuses.append(('task engine', 'ok', 'idle'))

    if disk_percent is not None:
        level = 'error' if disk_percent >= 95 else ('warn' if disk_percent >= 85 else 'ok')
        statuses.append(('disk usage', level, f"{disk_percent:.1f}% used"))

    return [(SELF_PREFIX + name, level, message) for name, level, message in statuses]
