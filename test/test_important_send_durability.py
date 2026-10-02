"""2026-09-28 stress test finding F5 (HIGH): a task's own TASK_RESULT and
its ADD_Task_history_database record used to be one-shot, fire-and-forget
sends (Engine._send_dict / add_task_history's own private_send call) --
confirmed live: start a task, kill the server while it's running, bring it
back -- the robot finishes the tree, but both sends fail (the connection
was down at that exact moment) and are never retried. 0 history rows
saved, "Run now" never hears the outcome, restart_on_failure never fires.

Engine._send_important_dict / _flush_pending_important_sends (engine.py)
queue a failed important send and retry it on the next successful
(re)connection (send_initial_data, the transport's own on_connected
callback) instead of just losing it.
"""
from unittest.mock import MagicMock

import pytest


def _make_engine(**kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    return Engine("secret", "proj-durability-test", **kwargs)


class TestSendImportantDict:
    def test_a_successful_send_is_not_queued(self):
        engine = _make_engine()
        sent = []
        engine.private_send = lambda msg, **k: sent.append(msg)
        engine.transport.websocket_connected = True

        engine._send_important_dict({"TASK_RESULT": {"task_id": "t1", "success": True}})

        assert len(sent) == 1
        assert engine._pending_important_sends == []

    def test_a_send_that_raises_is_queued_not_lost(self):
        engine = _make_engine()
        engine.private_send = MagicMock(side_effect=BrokenPipeError("connection reset"))

        engine._send_important_dict({"TASK_RESULT": {"task_id": "t1", "success": True}})

        assert len(engine._pending_important_sends) == 1
        assert engine._pending_important_sends[0]["TASK_RESULT"]["task_id"] == "t1"

    def test_a_send_that_succeeds_locally_but_transport_reports_disconnected_is_also_queued(self):
        """Confirmed live: websocket-client's send() to a socket the OS
        hasn't yet reported as dead can appear to succeed locally without
        the message ever arriving -- websocket_connected is the extra
        signal this codebase already relies on for exactly this gap."""
        engine = _make_engine()
        sent = []
        engine.private_send = lambda msg, **k: sent.append(msg)  # "succeeds"
        engine.transport.websocket_connected = False  # ...but we know we're down

        engine._send_important_dict({"TASK_RESULT": {"task_id": "t2"}})

        assert len(sent) == 1  # it did attempt the send
        assert len(engine._pending_important_sends) == 1  # but didn't trust it

    def test_queued_messages_are_flushed_on_reconnect_in_order(self):
        engine = _make_engine()
        engine.private_send = MagicMock(side_effect=BrokenPipeError("down"))
        engine._send_important_dict({"TASK_RESULT": {"task_id": "first"}})
        engine._send_important_dict({"TASK_RESULT": {"task_id": "second"}})
        assert len(engine._pending_important_sends) == 2

        delivered = []
        engine.private_send = lambda msg, **k: delivered.append(msg)
        engine.transport.websocket_connected = True

        engine._flush_pending_important_sends()

        assert len(delivered) == 2
        assert '"first"' in delivered[0]
        assert '"second"' in delivered[1]
        assert engine._pending_important_sends == []

    def test_a_message_that_fails_again_on_flush_stays_queued_not_dropped(self):
        engine = _make_engine()
        engine.private_send = MagicMock(side_effect=BrokenPipeError("down"))
        engine._send_important_dict({"TASK_RESULT": {"task_id": "stubborn"}})

        engine._flush_pending_important_sends()  # still failing

        assert len(engine._pending_important_sends) == 1
        assert engine._pending_important_sends[0]["TASK_RESULT"]["task_id"] == "stubborn"

    def test_flushing_an_empty_queue_does_nothing_and_never_raises(self):
        engine = _make_engine()
        engine.private_send = MagicMock(side_effect=AssertionError("must not be called"))
        engine._flush_pending_important_sends()  # must not raise
        assert engine._pending_important_sends == []


class TestAddTaskHistoryIsDurable:
    def test_a_history_record_lost_mid_send_is_recovered_on_the_next_connect(self):
        engine = _make_engine()
        engine.private_send = MagicMock(side_effect=BrokenPipeError("server restarted mid-task"))

        engine.add_task_history({
            "input_data": {"task_id": "abc"}, "output_data": {"success": True},
            "type": "bt_task", "created_at": "2026-09-28T00:00:00",
        })

        assert len(engine._pending_important_sends) == 1
        # The exact scenario confirmed live: server comes back, the robot
        # reconnects, send_initial_data (the on_connected callback) fires.
        delivered = []
        engine.private_send = lambda msg, **k: delivered.append(msg)
        engine.transport.websocket_connected = True
        engine.local_database.dashboard_receive = lambda *a, **k: None  # unrelated side effect, no-op here

        engine.send_initial_data()

        assert len(delivered) == 1
        assert "ADD_Task_history_database" in delivered[0]
        assert '"abc"' in delivered[0]
        assert engine._pending_important_sends == []

    def test_a_history_record_sent_while_connected_is_not_queued_at_all(self):
        engine = _make_engine()
        sent = []
        engine.private_send = lambda msg, **k: sent.append(msg)
        engine.transport.websocket_connected = True

        engine.add_task_history({
            "input_data": {}, "output_data": {"success": True}, "type": "bt_task",
            "created_at": "2026-09-28T00:00:00",
        })

        assert len(sent) == 1
        assert engine._pending_important_sends == []


class TestRunTaskResultSurvivesADisconnect:
    """The closest possible unit-level reproduction of the exact live
    scenario: a task finishes while _send_important_dict's send fails, and
    the result still reaches the server once the connection is confirmed
    usable again -- not lost, not silently dropped."""

    def test_task_result_queued_during_outage_is_delivered_on_reconnect(self):
        from xparo.bt_engine.executor import BehaviorTreeExecutor
        from xparo.bt_engine import run_task

        engine = _make_engine()
        engine.bt_executor = BehaviorTreeExecutor(node=None, engine=MagicMock())
        engine.private_send = MagicMock(side_effect=BrokenPipeError("connection was down"))

        run_task.handle_run_task(
            engine.bt_executor,
            {"task_id": "outage-task", "tree_xml": "<AlwaysSuccess/>", "blackboard": {}, "save_task_history": False},
            engine._send_important_dict,
            xparo_stage="development",
        )

        # The result didn't just vanish -- it's queued.
        assert len(engine._pending_important_sends) == 1
        assert engine._pending_important_sends[0]["TASK_RESULT"]["task_id"] == "outage-task"
        assert engine._pending_important_sends[0]["TASK_RESULT"]["success"] is True

        # Connection recovers; the next reconnect flushes it through.
        delivered = []
        engine.private_send = lambda msg, **k: delivered.append(msg)
        engine.transport.websocket_connected = True
        engine.local_database.dashboard_receive = lambda *a, **k: None

        engine.send_initial_data()

        assert len(delivered) == 1
        assert "outage-task" in delivered[0]
        assert engine._pending_important_sends == []
