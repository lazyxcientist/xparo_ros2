"""AI task calling on the robot -- the same flow as the dashboard's Tasks
page chat. A question published on /xparo/ask_task goes to the XPARO
server (ask_bot_task_call), whose AI picks the task whose AI prompt
matches. The pick comes back as a task_call {task_id, title, params,
timeout_s} and is offered like the dashboard's "Call now" card: published
on /xparo/task_call, then run when "call" arrives on /xparo/task_call_confirm
or automatically after timeout_s (15 s); "cancel" ends a test run without
calling anything. Only one offer is pending at a time; a new one replaces it.
"""
import threading

DEFAULT_TIMEOUT_S = 15


class TaskCallOffer:
    def __init__(self, run_task, publish, timer_factory=threading.Timer):
        # run_task(task_id, params) runs a synced task (Engine.run_task_from_topic);
        # publish(dict) reports the offer's state (pending/called/cancelled/expired).
        self._run_task = run_task
        self._publish = publish
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._pending = None
        self._timer = None

    def offer(self, call):
        if not isinstance(call, dict) or not call.get("task_id"):
            return
        try:
            timeout_s = float(call.get("timeout_s") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            timeout_s = DEFAULT_TIMEOUT_S
        call = {"task_id": str(call["task_id"]), "title": call.get("title") or "",
                "params": call.get("params") if isinstance(call.get("params"), dict) else {},
                "timeout_s": timeout_s}
        with self._lock:
            self._stop_timer()
            self._pending = call
            self._timer = self._timer_factory(timeout_s, self._expire, args=(call,))
            self._timer.daemon = True
            self._timer.start()
        self._publish({**call, "status": "pending"})

    def confirm(self):
        """"Call now". Returns False when nothing is pending."""
        call = self._take()
        if call is None:
            return False
        self._publish({**call, "status": "called"})
        self._run_task(call["task_id"], call["params"] or None)
        return True

    def cancel(self):
        call = self._take()
        if call is None:
            return False
        self._publish({**call, "status": "cancelled"})
        return True

    def _expire(self, call):
        # the countdown ended with no answer: call it, like the dashboard card
        with self._lock:
            if self._pending is not call:
                return
            self._pending = None
            self._timer = None
        self._publish({**call, "status": "called"})
        self._run_task(call["task_id"], call["params"] or None)

    def _take(self):
        with self._lock:
            call, self._pending = self._pending, None
            self._stop_timer()
            return call

    def _stop_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
