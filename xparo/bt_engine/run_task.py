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


def handle_run_task(executor, val, send_response, add_task_history=None):
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
