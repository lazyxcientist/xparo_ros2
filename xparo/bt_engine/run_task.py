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
import time
from datetime import datetime

from py_trees import common

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


def handle_run_task(executor, val, send_response, add_task_history=None, xparo_stage="production"):
    """Blocks the calling thread until the tree reaches a final status or
    hits its own tick budget -- callers that can't afford to block their
    dispatch loop must run this in its own thread (engine.py's
    on_ws_message does exactly that, one thread per task, matching
    RUN_COMMAND's established pattern).
    """
    task_id = val.get("task_id")
    tree_xml = val.get("tree_xml", "")
    blackboard = dict(val.get("blackboard") or {})
    save_task_history = bool(val.get("save_task_history"))
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
            "success": False,
            "duration_s": 0.0,
            "stage_mismatch": True,
            "error": (
                f"This robot is configured for xparo_stage={xparo_stage!r} and "
                f"cannot run a {task_stage!r}-stage task."
            ),
        }})
        return

    started = time.monotonic()
    try:
        status, final_blackboard = executor.run(tree_xml, blackboard=blackboard)
        success = status == common.Status.SUCCESS
    except Exception as e:
        success = False
        final_blackboard = {**blackboard, "_error": str(e)}
    duration_s = time.monotonic() - started

    send_response({"TASK_RESULT": {
        "task_id": task_id,
        "success": success,
        "duration_s": duration_s,
        # Empty string (not omitted) for a clean FAILURE with no exception
        # -- distinguishes "the tree deliberately returned FAILURE" from
        # "something raised", which final_blackboard already had this
        # whole time but never forwarded past this point.
        "error": final_blackboard.get("_error", ""),
    }})

    if save_task_history and add_task_history is not None:
        # Reuses Engine.add_task_history / Django's existing
        # ADD_Task_history_database handler as-is (apps/analytics/
        # data_analyis.py already links the created row to this robot) --
        # Task_history has no FK back to the Services row that produced
        # it, so task_id/blackboard travel in input_data instead, the only
        # slot available for that correlation.
        add_task_history({
            "input_data": {"task_id": task_id, "blackboard": blackboard},
            "output_data": {"success": success, "blackboard": final_blackboard, "duration_s": duration_s},
            "type": "bt_task",
            "created_at": datetime.now().isoformat(),
        })
