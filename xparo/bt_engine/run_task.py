"""Behaviour Tree redesign Phase 11: the robot-side half of RUN_TASK ->
TASK_RESULT -- where "add a task in the dashboard, have a robot run it"
first becomes real. Mirrors remote_ops.py's handler convention exactly (a
plain function taking a send_response(dict) callback, not a method, so
engine.py can run it in its own thread the same way it already does for
RUN_COMMAND) even though it lives in bt_engine/ rather than remote_ops.py
-- this is BT-execution-specific, not a general exec/file-transfer/teleop
primitive those handlers share.

blackboard_mapping is always resolved into a concrete {name: value} dict
*before* this ever runs -- this handler never needs to know about mapping
*types*, just "here's your resolved blackboard". Two callers resolve it
two different ways, both producing the identical `val` shape this
function reads: engine.py's own "RUN_TASK" on_ws_message branch, where
Django resolved it server-side (apps/analytics/data_analyis.py's
resolve_blackboard_mapping) before ever sending it over the websocket;
and engine.py's run_task_from_topic (triggered by /xparo/run_task,
xparo_ros.py), where this robot resolves it itself from its own already-
synced local files (bt_engine.task_sync.resolve_blackboard) with zero
Django contact at trigger time. handle_run_task itself doesn't know or
care which path produced its `val`.
"""
import threading
import time
import uuid
from datetime import datetime

from .executor import make_report

# Cascade table: which Task stages (apps/analytics/models.py's
# TaskStageChoices, outer repo) a robot configured with a given
# xparo_stage is allowed to run. development is the least restrictive (a
# dev robot is expected to run anything, including tasks nobody's promoted
# yet) through production, the most restrictive (a production robot must
# never run a task someone's still iterating on). Matches the exact
# cascade the user specified: development runs all four; testing runs
# testing/review/production; review runs review/production; production
# runs production only.
_STAGE_ORDER = ["development", "testing", "review", "production"]
ALLOWED_TASK_STAGES = {
    robot_stage: set(_STAGE_ORDER[i:])
    for i, robot_stage in enumerate(_STAGE_ORDER)
}


DEFAULT_TIMEOUT_S = 900  # Services.timeout's own default (Django)

# Outcomes a retry can't fix -- Django's restart_on_failure skips these
# instead of re-dispatching the same broken task in a tight loop.
NOT_RETRYABLE = {"stage_mismatch", "invalid_tree", "no_executor", "unknown_task", "cancelled"}

_active_lock = threading.Lock()
_active_runs = {}  # run_id -> {"task_id", "event", "started", "reason"}


def active_runs():
    """[{run_id, task_id, running_s}] for every task running right now."""
    now = time.monotonic()
    with _active_lock:
        return [{"run_id": run_id, "task_id": info["task_id"], "running_s": round(now - info["started"], 1)}
                for run_id, info in _active_runs.items()]


def cancel_task(task_id=None, run_id=None, reason="cancelled from the dashboard"):
    """Asks running task(s) to stop: one run by run_id, or every run of
    task_id. Returns the run_ids that were asked. The tree halts at its
    next tick (running nodes get terminate() so they can clean up)."""
    cancelled = []
    with _active_lock:
        for rid, info in _active_runs.items():
            if (run_id and rid == run_id) or (not run_id and task_id and info["task_id"] == task_id):
                info["reason"] = reason
                info["event"].set()
                cancelled.append(rid)
    return cancelled


def _new_run_id():
    return uuid.uuid4().hex[:12]


def _json_safe(value, depth=0):
    """Blackboard values end up in JSON (TASK_RESULT, task history); a
    node may have stored something that isn't serializable."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if depth > 6:
        return repr(value)[:200]
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return repr(value)[:500]


def _blackboard_diff(before, after):
    added = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)
    changed = sorted(k for k in after if k in before and after[k] != before[k])
    return {"added": added, "changed": changed, "removed": removed}


def result_message(task_id, run_id, outcome, error, explanation="", **extra):
    """A TASK_RESULT for a run that never started (no executor, unknown
    task, empty tree) -- same shape as a real one."""
    return {"TASK_RESULT": {
        "task_id": task_id, "run_id": run_id, "success": False, "duration_s": 0.0,
        "outcome": outcome, "retryable": outcome not in NOT_RETRYABLE,
        "error": error, "explanation": explanation, **extra,
    }}


def handle_run_task(executor, val, send_response, add_task_history=None, xparo_stage="production",
                    subtree_resolver=None, on_started=None):
    """Blocks the calling thread until the tree reaches a final status,
    its time limit, or a cancel -- callers that can't afford to block
    their dispatch loop must run this in its own thread (engine.py's
    on_ws_message does exactly that, one thread per task, matching
    RUN_COMMAND's established pattern).
    """
    task_id = val.get("task_id")
    run_id = val.get("run_id") or _new_run_id()
    tree_xml = val.get("tree_xml", "") or ""
    blackboard = dict(val.get("blackboard") or {})
    save_task_history = bool(val.get("save_task_history"))
    tree_name = val.get("tree_name") or ""
    context = {
        "task_title": val.get("task_title") or "",
        "tree_name": tree_name,
        "trigger": val.get("trigger") or "",
        "attempt": val.get("attempt") or 1,
    }
    # Unknown/missing stage (a stale locally-cached task from before this
    # feature, or a malformed dispatch) is treated as "development" -- NOT
    # "production". This is the task's own stage, the opposite direction
    # from xparo_stage's "production" default just above: "production"
    # here would mean an ambiguous/unlabeled task is trusted as
    # production-ready and runnable by every robot including strict
    # production ones, exactly backwards. "development" is only ever
    # runnable by the most permissive robots, so a labeling gap can only
    # make a task run in *fewer* places, never more.
    task_stage = val.get("stage") or "development"

    if task_stage not in ALLOWED_TASK_STAGES.get(xparo_stage, set()):
        # stage_mismatch is a distinct flag, not just success=False --
        # Django's TASK_RESULT handler skips restart_on_failure for it
        # specifically (apps/analytics/data_analyis.py), since retrying on
        # this exact same robot is guaranteed to hit the identical
        # rejection every time, unlike a normal execution failure that
        # might genuinely succeed on retry. Without that check this would
        # be an infinite dispatch loop, not a one-time rejection.
        send_response({"TASK_RESULT": {
            "task_id": task_id,
            "run_id": run_id,
            "success": False,
            "duration_s": 0.0,
            "stage_mismatch": True,
            "outcome": "stage_mismatch",
            "retryable": False,
            "error": (
                f"This robot is configured for xparo_stage={xparo_stage!r} and "
                f"cannot run a {task_stage!r}-stage task."
            ),
            "explanation": (
                f"Promote the task to a stage this robot accepts, or run it on a robot whose "
                f"xparo_stage allows {task_stage!r} tasks."
            ),
        }})
        return

    raw_timeout = val.get("timeout_s", DEFAULT_TIMEOUT_S)
    try:
        timeout_s = float(raw_timeout) if raw_timeout not in (None, "") else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    timeout_s = timeout_s if timeout_s > 0 else None  # 0 = no time limit

    event = threading.Event()
    with _active_lock:
        _active_runs[run_id] = {"task_id": task_id, "event": event, "started": time.monotonic(), "reason": ""}
    # send_response gets exactly one message per run (the TASK_RESULT);
    # "it started" goes through its own callback so the dashboard can show
    # the run as live (and offer Cancel) instead of guessing.
    if on_started is not None:
        try:
            on_started({"TASK_STARTED": {
                "task_id": task_id, "run_id": run_id, "timeout_s": timeout_s,
                "started_at": datetime.now().astimezone().isoformat(), **context,
            }})
        except Exception as e:  # the report still matters more than the ping
            print(f"[run_task] TASK_STARTED not sent: {e}")

    try:
        delay = float(val.get("start_delay_s") or 0)
    except (TypeError, ValueError):
        delay = 0
    if delay > 0:
        event.wait(min(delay, 300))

    try:
        if not tree_xml.strip():
            where = f"Behaviour tree {tree_name!r}" if tree_name else "The project's main behaviour tree"
            report = make_report("invalid_tree", (
                f"{where} is empty or missing on this robot -- open it in the Behaviour editor, add nodes "
                f"and save, then make sure the robot is connected so it syncs"), blackboard, run_id)
        elif event.is_set():
            report = make_report("cancelled", "Cancelled before it started", blackboard, run_id)
        else:
            report = executor.run_with_trace(
                tree_xml, blackboard=blackboard, timeout_s=timeout_s, cancel_event=event,
                subtrees=val.get("subtrees") or None, subtree_resolver=subtree_resolver, run_id=run_id,
                live_context={"task_id": task_id, "tree_name": tree_name, "task_title": context["task_title"]},
            )
    finally:
        with _active_lock:
            info = _active_runs.pop(run_id, {})
    if report.get("outcome") == "cancelled" and info.get("reason"):
        report["explanation"] = f"The task was cancelled ({info['reason']})."
    failed = report.get("failed_node")
    if report.get("outcome") == "failed" and failed:
        report["explanation"] = f"The tree finished with FAILURE at {failed['name']} ({failed['tag']}): {failed['reason']}."

    final_blackboard = _json_safe(report.get("blackboard") or {})
    report["blackboard"] = final_blackboard
    report["blackboard_diff"] = _blackboard_diff(blackboard, final_blackboard)
    report.update(context)
    success = bool(report.get("success"))
    duration_s = float(report.get("duration_s") or 0.0)
    outcome = report.get("outcome") or ("success" if success else "failed")

    send_response({"TASK_RESULT": {
        "task_id": task_id,
        "run_id": run_id,
        "success": success,
        "duration_s": duration_s,
        # Empty string (not omitted) for a clean FAILURE with no exception
        # -- distinguishes "the tree deliberately returned FAILURE" from
        # "something went wrong running it". Which node failed and why
        # is in failed_node/explanation either way.
        "error": "" if outcome in ("success", "failed") else report.get("error", ""),
        "outcome": outcome,
        "retryable": outcome not in NOT_RETRYABLE,
        "explanation": report.get("explanation", ""),
        "failed_node": report.get("failed_node"),
        "stats": report.get("stats") or {},
        **context,
    }})

    if save_task_history and add_task_history is not None:
        # Reuses Engine.add_task_history / Django's existing
        # ADD_Task_history_database handler as-is (apps/analytics/
        # data_analyis.py already links the created row to this robot) --
        # Task_history has no FK back to the Services row that produced
        # it, so task_id/blackboard travel in input_data instead, the only
        # slot available for that correlation. output_data.report is the
        # full execution report the task history popup draws.
        history_report = {k: v for k, v in report.items() if k not in ("blackboard",)}
        add_task_history({
            "input_data": {"task_id": task_id, "run_id": run_id, "blackboard": _json_safe(blackboard),
                           "stage": task_stage, "timeout_s": timeout_s, **context},
            "output_data": {"success": success, "blackboard": final_blackboard, "duration_s": duration_s,
                            "outcome": outcome, "error": report.get("error", ""), "report": _json_safe(history_report)},
            "type": "bt_task",
            "created_at": report.get("started_at") or datetime.now().astimezone().isoformat(),
        })
