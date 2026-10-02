"""Behaviour Tree redesign Phase 9: BehaviorTreeExecutor -- builds a
py_trees tree from a stored fragment + an already-resolved blackboard
dict, ticks it to completion, and relays every real status change to the
dashboard's existing live-tree visualization in the exact shape
Xparo.navigation_callback (xparo_ros.py) already produces from Nav2's
BehaviorTreeLog, so that consumer needs zero changes to also show locally-
executed trees.

Composable class owned by the Xparo rclpy node, not its own Node --
mirrors rosbag_control.py's RosbagControl exactly (constructed with a
reference to the host node plus whatever else it needs, using the node's
own create_client/publisher/timer rather than spinning up a second node).
`engine` is the already-constructed Engine instance (xparo_ros.py builds
this after Engine, same ordering RosbagControl doesn't need but this does,
since it must call engine.add_live_update).

Deliberately uses a plain Python dict as the blackboard, not py_trees' own
Blackboard/Client system -- that's a process-wide global keyed by string
paths with its own key-registration ceremony, which would need careful
per-run namespacing to avoid one task's variables leaking into a
concurrently-running second task's tree on the same robot process. A
fresh dict per run() call is simpler and fully isolated by construction.

run() is synchronous and blocks the calling thread until the tree
reaches SUCCESS/FAILURE or max_ticks is hit -- it does not spawn its own
thread. Phase 11's RUN_TASK handler is the one that needs this off the
WS-dispatch thread, and does so the same way remote_ops.py's RUN_COMMAND
already does (threading.Thread(target=...)), rather than this class
managing its own thread and hiding that decision from callers that might
want it synchronous (as every test in test_bt_engine.py does).

run_with_trace() is what tasks use: it never raises, and returns a full
execution report -- outcome, a plain-language explanation, the node that
failed and why, per-node tick counts and timings, a status timeline -- so
the task history can answer "what happened?" without anyone reading robot
logs. It also enforces the task's time limit, can be cancelled, and halts
every running node cleanly whenever a run ends early.
"""
import platform
import time
import traceback
from collections import deque
from datetime import datetime

import py_trees
from py_trees import common

from . import tree_builder
from .builtins import TrackedBlackboard
from .xml_parser import TreeParseError

REPORT_VERSION = 1
MAX_REPORT_NODES = 500
MAX_TIMELINE_HEAD = 400
MAX_TIMELINE_TAIL = 150

OUTCOME_TEXT = {
    "success": "The tree finished with SUCCESS.",
    "failed": "The tree finished with FAILURE.",
    "invalid_tree": "The tree couldn't start because it has errors.",
    "node_error": "A node crashed while running, so the task was stopped.",
    "timeout": "The task was still running when its time limit ran out, so it was stopped.",
    "tick_limit": "The task was still running after its tick budget, so it was stopped.",
    "cancelled": "The task was cancelled while it was running.",
    "engine_error": "The robot's behaviour tree engine hit an internal error.",
}


def make_report(outcome, error, blackboard=None, run_id=None, explanation=None):
    """The report for a run that never reached the tree (nothing to trace)."""
    now = datetime.now().astimezone().isoformat()
    return {
        "version": REPORT_VERSION, "run_id": run_id, "started_at": now, "finished_at": now, "duration_s": 0.0,
        "outcome": outcome, "success": False, "status": "INVALID", "error": error,
        "explanation": explanation or OUTCOME_TEXT.get(outcome, ""), "blackboard": dict(blackboard or {}),
        "problems": [], "failed_node": None, "nodes": [], "timeline": [],
        "stats": {"nodes_total": 0, "nodes_ticked": 0, "node_ticks": 0, "tree_ticks": 0},
    }


class _NodeStats:
    __slots__ = ("ticks", "runs", "first", "last", "active", "run_started", "last_status", "note", "halts")

    def __init__(self):
        self.ticks = 0
        self.runs = 0
        self.first = None
        self.last = None
        self.active = 0.0
        self.run_started = None
        self.last_status = None
        self.note = ""
        self.halts = 0


class _TraceVisitor(py_trees.visitors.VisitorBase):
    """Called by py_trees for every node each time it's ticked (including
    several times per tree tick inside Repeat/Retry loops)."""

    def __init__(self, t0):
        super().__init__(full=False)
        self.t0 = t0
        self.stats = {}

    def run(self, behaviour):
        now = time.monotonic() - self.t0
        s = self.stats.get(behaviour.id)
        if s is None:
            s = self.stats[behaviour.id] = _NodeStats()
        s.ticks += 1
        if s.last_status != common.Status.RUNNING:
            s.runs += 1
            s.run_started = now
        if s.first is None:
            s.first = now
        s.last = now
        status = behaviour.status
        if status != common.Status.RUNNING and s.run_started is not None:
            s.active += now - s.run_started
            s.run_started = None
        s.last_status = status
        if behaviour.feedback_message:
            s.note = str(behaviour.feedback_message)[:500]

    def mark_halted(self, behaviour):
        """A RUNNING node its parent (or the end of the run) stopped."""
        s = self.stats.get(behaviour.id)
        if s is None or s.last_status != common.Status.RUNNING:
            return
        now = time.monotonic() - self.t0
        if s.run_started is not None:
            s.active += now - s.run_started
            s.run_started = None
        s.halts += 1
        s.last_status = "HALTED"


class BehaviorTreeExecutor:
    def __init__(self, node, engine):
        self.node = node
        self.engine = engine

    def run(self, xml_fragment, blackboard=None, tick_rate_hz=10, max_ticks=1000):
        """Returns (final_status, blackboard) -- the caller (Phase 11) needs
        the post-run blackboard back (e.g. to report resolved values in
        TASK_RESULT), not just the status. Build errors and node crashes
        raise, as they always have; tasks use run_with_trace instead.
        """
        blackboard = {} if blackboard is None else blackboard
        root = tree_builder.build_tree(xml_fragment, blackboard, ros_node=self.node)
        state = self._tick_loop(root, tick_rate_hz=tick_rate_hz, max_ticks=max_ticks)
        if state["exception"] is not None:
            raise state["exception"]
        return root.status, blackboard

    def run_with_trace(self, xml_fragment, blackboard=None, tick_rate_hz=10, timeout_s=None, max_ticks=None,
                       cancel_event=None, subtrees=None, subtree_resolver=None, run_id=None, live_context=None):
        """Runs a tree and returns its execution report (a JSON-safe dict,
        see _report). Never raises."""
        started_wall = time.time()
        blackboard = TrackedBlackboard(blackboard or {})
        base = {
            "version": REPORT_VERSION,
            "run_id": run_id,
            "started_at": datetime.fromtimestamp(started_wall).astimezone().isoformat(),
            "timeout_s": timeout_s,
            "tick_rate_hz": tick_rate_hz,
            "engine": {"py_trees": _py_trees_version(), "python": platform.python_version()},
        }
        try:
            problems = tree_builder.validate_tree(xml_fragment, subtrees=subtrees, subtree_resolver=subtree_resolver)
            errors = [p for p in problems if p["level"] == "error"]
            if errors:
                return self._early_report(base, blackboard, problems, errors, started_wall)
            try:
                root = tree_builder.build_tree(xml_fragment, blackboard, ros_node=self.node,
                                               subtrees=subtrees, subtree_resolver=subtree_resolver)
            except (tree_builder.TreeBuildError, TreeParseError) as e:
                problem = {"level": "error", "message": getattr(e, "reason", str(e)), "path": getattr(e, "path", ""),
                           "tag": getattr(e, "tag", ""), "node": ""}
                return self._early_report(base, blackboard, problems + [problem], [problem], started_wall)
            state = self._tick_loop(root, tick_rate_hz=tick_rate_hz, max_ticks=max_ticks,
                                    timeout_s=timeout_s, cancel_event=cancel_event, trace=True, run_id=run_id,
                                    live_context=live_context)
            return self._report(base, root, blackboard, problems, state, started_wall)
        except Exception as e:  # a bug in the engine itself -- still report it
            report = dict(base)
            report.update(self._outcome_fields("engine_error", f"{type(e).__name__}: {e}"))
            report.update({
                "status": "INVALID", "blackboard": dict(blackboard), "problems": [], "nodes": [], "timeline": [],
                "stats": {}, "failed_node": None, "traceback": _short_traceback(e),
                "finished_at": datetime.now().astimezone().isoformat(), "duration_s": time.time() - started_wall,
            })
            return report

    # ------------------------------------------------------------ ticking

    def _tick_loop(self, root, tick_rate_hz=10, max_ticks=None, timeout_s=None, cancel_event=None, trace=False,
                   run_id=None, live_context=None):
        tree = py_trees.trees.BehaviourTree(root)
        t0 = time.monotonic()
        visitor = _TraceVisitor(t0) if trace else None
        if visitor is not None:
            tree.visitors.append(visitor)
        timeline = [] if trace else None
        tail = deque(maxlen=MAX_TIMELINE_TAIL) if trace else None
        dropped = [0]
        previous_status = {}

        def _record_change(behaviour, prev, curr):
            if timeline is None or getattr(behaviour, "xparo_synthetic", False):
                return
            event = [round(time.monotonic() - t0, 3), behaviour.name, getattr(behaviour, "xparo_tag", ""),
                     prev.name, curr.name]
            if len(timeline) < MAX_TIMELINE_HEAD:
                timeline.append(event)
            else:
                if len(tail) == tail.maxlen:
                    dropped[0] += 1
                tail.append(event)

        def _on_post_tick(ticked_tree):
            # root.iterate() walks the *whole* tree structure, including
            # siblings never actually reached this tick (e.g. later steps
            # in a Sequence that never got past an earlier RUNNING one) --
            # those sit at py_trees' own default Status.INVALID, same as
            # "never seen before". Defaulting the lookup to INVALID (not
            # None) makes that comparison correctly a no-op instead of a
            # spurious "None -> INVALID" event on every node that merely
            # exists in the tree, not just the ones actually ticked.
            for behaviour in ticked_tree.root.iterate():
                prev = previous_status.get(behaviour.id, common.Status.INVALID)
                curr = behaviour.status
                if prev != curr:
                    self._emit_live_update(behaviour, prev, curr, run_id, live_context)
                    _record_change(behaviour, prev, curr)
                    previous_status[behaviour.id] = curr
                if visitor is not None and curr == common.Status.INVALID:
                    visitor.mark_halted(behaviour)

        tree.add_post_tick_handler(_on_post_tick)

        interval = (1.0 / tick_rate_hz) if tick_rate_hz else 0
        deadline = (t0 + timeout_s) if timeout_s else None
        ticks = 0
        stop_reason = None
        exception = None
        running_at_stop = []
        while True:
            if cancel_event is not None and cancel_event.is_set():
                stop_reason = "cancelled"
                break
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "timeout"
                break
            if max_ticks is not None and ticks >= max_ticks:
                stop_reason = "tick_limit"
                break
            try:
                tree.tick()
            except Exception as e:
                exception = e
                stop_reason = "node_error"
                ticks += 1
                break
            ticks += 1
            if root.status in (common.Status.SUCCESS, common.Status.FAILURE):
                break
            if interval:
                if cancel_event is not None:
                    cancel_event.wait(interval)
                else:
                    time.sleep(interval)

        final_status = root.status
        if stop_reason is not None:
            running_at_stop = [b for b in _preorder(root) if b.status == common.Status.RUNNING
                               and not getattr(b, "xparo_synthetic", False)]
            if stop_reason != "tick_limit":
                # Halt whatever is still running (terminate() lets nodes
                # cancel goals, kill processes...), then report the halt
                # to the live view so nothing stays highlighted RUNNING.
                try:
                    root.stop(common.Status.INVALID)
                except Exception as halt_error:  # keep the original story
                    running_at_stop.append(halt_error)
                try:
                    _on_post_tick(tree)
                except Exception:
                    pass
        timeline_out = None
        if timeline is not None:
            timeline_out = timeline + list(tail)
        return {
            "status": final_status, "ticks": ticks, "stop_reason": stop_reason, "exception": exception,
            "visitor": visitor, "timeline": timeline_out, "timeline_dropped": dropped[0],
            "running_at_stop": running_at_stop, "elapsed": time.monotonic() - t0,
        }

    # ------------------------------------------------------------ report

    def _outcome_fields(self, outcome, error=""):
        return {"outcome": outcome, "success": outcome == "success", "error": error,
                "explanation": OUTCOME_TEXT.get(outcome, "")}

    def _early_report(self, base, blackboard, problems, errors, started_wall):
        report = dict(base)
        lines = [f"{p['message']}" + (f" (at {p['path']})" if p.get("path") else "") for p in errors]
        report.update(self._outcome_fields("invalid_tree", lines[0] if len(lines) == 1 else
                                           f"{len(lines)} problems: " + "; ".join(lines)))
        first = errors[0]
        report.update({
            "status": "INVALID", "blackboard": dict(blackboard), "problems": problems, "nodes": [],
            "timeline": [], "stats": {"nodes_total": 0, "nodes_ticked": 0, "node_ticks": 0, "tree_ticks": 0},
            "failed_node": {"name": first.get("node", ""), "tag": first.get("tag", ""), "path": first.get("path", ""),
                            "reason": first["message"]} if first.get("path") else None,
            "finished_at": datetime.now().astimezone().isoformat(), "duration_s": time.time() - started_wall,
        })
        return report

    def _report(self, base, root, blackboard, problems, state, started_wall):
        visitor = state["visitor"]
        stats = visitor.stats
        rows = _node_rows(root, stats)
        status = state["status"]
        stop = state["stop_reason"]
        if stop is not None:
            outcome = stop
        elif status == common.Status.SUCCESS:
            outcome = "success"
        else:
            outcome = "failed"

        report = dict(base)
        failed_node = None
        extra = {}
        if outcome == "failed":
            failed_node = _failure_path(root, stats)
            error = f"{failed_node['name']} failed: {failed_node['reason']}" if failed_node else "the tree returned FAILURE"
        elif outcome == "node_error":
            exc = state["exception"]
            culprit = _culprit(exc)
            message = f"{type(exc).__name__}: {exc}"
            if culprit is not None:
                failed_node = _describe_node(culprit, f"crashed with {message}")
                error = f"{failed_node['name']} crashed: {message}"
            else:
                error = message
            extra["traceback"] = _short_traceback(exc)
        elif outcome in ("timeout", "tick_limit", "cancelled"):
            waiting = [b for b in state["running_at_stop"] if not isinstance(b, Exception)]
            leaves = [b for b in waiting if not getattr(b, "children", None)] or waiting
            what = {"timeout": f"it hit its {base.get('timeout_s') or 0:g} s time limit",
                    "tick_limit": f"it used its {state['ticks']} tick budget",
                    "cancelled": "it was cancelled"}[outcome]
            if leaves:
                failed_node = _describe_node(leaves[-1], f"was still running when {what}")
                error = f"Stopped because {what}, while {failed_node['name']} was still running"
            else:
                error = f"Stopped because {what}"
            halt_errors = [b for b in state["running_at_stop"] if isinstance(b, Exception)]
            if halt_errors:
                problems = problems + [{"level": "warning", "message": f"halting the tree raised {halt_errors[0]!r}",
                                        "path": "", "tag": "", "node": ""}]
        else:
            error = ""

        real_rows = [r for r in rows if r is not None]
        report.update(self._outcome_fields(outcome, error))
        report.update(extra)
        report.update({
            "status": status.name,
            "blackboard": dict(blackboard),
            "changed_keys": sorted(k for k, v in getattr(blackboard, "versions", {}).items() if v > 1),
            "problems": problems,
            "failed_node": failed_node,
            "nodes": real_rows[:MAX_REPORT_NODES],
            "nodes_truncated": max(0, len(real_rows) - MAX_REPORT_NODES),
            "timeline": state["timeline"],
            "timeline_dropped": state["timeline_dropped"],
            "stats": _totals(real_rows, state["ticks"]),
            "finished_at": datetime.now().astimezone().isoformat(),
            "duration_s": time.time() - started_wall,
        })
        return report

    def _emit_live_update(self, behaviour, prev_status, curr_status, run_id=None, live_context=None):
        now = time.time()
        data = {
            # task_id/tree_name/task_title (run_task.py) -- lets the
            # Behaviour editor light up only the tree that's running.
            **(live_context or {}),
            # Finding F8 (MEDIUM): confirmed live -- with two tasks running
            # concurrently (e.g. a scheduled task overlapping a manual
            # "run tree" from the dashboard), their live node-status
            # updates were indistinguishable on the wire, so the live
            # canvas could highlight nodes from the WRONG run. run_id is
            # None for the untraced run() path (dashboard "preview" runs,
            # not a dispatched task), which is fine -- only run_with_trace
            # (the real task-execution path) ever has one to give.
            "run_id": run_id,
            "node_name": behaviour.name,
            # The registration/type name (tree_builder.py sets
            # node.xparo_tag to the XML tag at build time), distinct from
            # node_name -- mirrors this project's own prior C++
            # RosTopicLogger exactly (node.name() vs
            # node.registrationName()). xparo_ros.py's navigation_callback
            # (Nav2's forwarded BehaviorTreeLog) still duplicates node_name
            # into this field -- that's Nav2's own message schema
            # genuinely having no separate field, not a choice made here.
            "node_type": getattr(behaviour, "xparo_tag", behaviour.name),
            "uid": str(behaviour.id),
            "prev": prev_status.name,
            "curr": curr_status.name,
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        }
        # The live view is a nice-to-have; a dropped connection must never
        # stop the robot's task.
        try:
            self.engine.add_live_update(data)
        except Exception as e:
            print(f"[bt_engine] live update not sent: {e}")


def _py_trees_version():
    try:
        from importlib.metadata import version
        return version("py_trees")
    except Exception:
        return ""


def _preorder(node):
    yield node
    for child in getattr(node, "children", []) or []:
        yield from _preorder(child)


def _real(node):
    """Unwraps the synthetic _skipIf/... wrapper to the node it gates."""
    if getattr(node, "xparo_synthetic", False):
        return node.decorated, node
    return node, None


def _status_name(value):
    if value is None:
        return "IDLE"
    return value if isinstance(value, str) else value.name


def _node_rows(root, stats):
    rows = []

    def walk(node, depth):
        node, gate = _real(node)
        s = stats.get(node.id)
        g = stats.get(gate.id) if gate is not None else None
        status = _status_name(s.last_status if s else None)
        note = s.note if s else ""
        if gate is not None and g is not None and gate.last_gate not in (None, "run"):
            # The condition short-circuited on its last check.
            status = "SKIPPED" if gate.last_gate == "skip" else _status_name(g.last_status)
            note = g.note
        elif gate is not None and g is not None and g.note and not note:
            note = g.note
        row = {
            "name": node.name,
            "tag": getattr(node, "xparo_tag", type(node).__name__),
            "depth": depth,
            "path": getattr(node, "xparo_path", node.name),
            "ticks": s.ticks if s else 0,
            "runs": s.runs if s else 0,
            "halts": s.halts if s else 0,
            "status": status,
            "note": note,
            "first_s": round(s.first, 3) if s and s.first is not None else None,
            "last_s": round(s.last, 3) if s and s.last is not None else None,
            "active_s": round(s.active, 3) if s else 0,
        }
        if gate is not None:
            row["conditions"] = gate.conditions()
            row["checked"] = g.ticks if g else 0
        rows.append(row)
        for child in getattr(node, "children", []) or []:
            walk(child, depth + 1)

    walk(root, 0)
    return rows


def _totals(rows, tree_ticks):
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "nodes_total": len(rows),
        "nodes_ticked": sum(1 for r in rows if r["ticks"] > 0),
        "nodes_skipped": counts.get("SKIPPED", 0),
        "nodes_never_reached": sum(1 for r in rows if r["ticks"] == 0 and r["status"] != "SKIPPED"),
        "node_ticks": sum(r["ticks"] for r in rows),
        "tree_ticks": tree_ticks,
        "by_status": counts,
    }


def _failure_path(root, stats):
    """Follows FAILURE down from the root to the node that caused it."""
    node, gate = _real(root)
    while True:
        if gate is not None and gate.last_gate in ("failure", "while"):
            return _describe_node(node, gate.feedback_message or "its condition failed")
        failing = []
        for child in getattr(node, "children", []) or []:
            real, child_gate = _real(child)
            candidate = child_gate if child_gate is not None else real
            s = stats.get(candidate.id)
            if s is not None and s.last_status == common.Status.FAILURE:
                failing.append((s.last or 0, real, child_gate))
        if not failing:
            s = stats.get(node.id)
            return _describe_node(node, _default_reason(node) or (s.note if s and s.note else "returned FAILURE"))
        _, node, gate = max(failing, key=lambda item: item[0])


def _default_reason(node):
    tag = getattr(node, "xparo_tag", "")
    if tag == "Inverter":
        return "Inverter turned its child's SUCCESS into FAILURE"
    if tag == "ForceFailure":
        return "ForceFailure always returns FAILURE"
    if tag == "AlwaysFailure":
        return "AlwaysFailure always returns FAILURE"
    return ""


def _describe_node(node, reason):
    return {"name": node.name, "tag": getattr(node, "xparo_tag", type(node).__name__),
            "path": getattr(node, "xparo_path", node.name), "reason": reason}


def _culprit(exc):
    """The deepest tree node on the exception's call stack."""
    culprit = None
    tb = exc.__traceback__ if exc is not None else None
    while tb is not None:
        candidate = tb.tb_frame.f_locals.get("self")
        if isinstance(candidate, py_trees.behaviour.Behaviour) and not getattr(candidate, "xparo_synthetic", False):
            culprit = candidate
        tb = tb.tb_next
    return culprit


def _short_traceback(exc, limit=12):
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    text = "".join(lines).splitlines()
    return "\n".join(text[-limit * 2:])
