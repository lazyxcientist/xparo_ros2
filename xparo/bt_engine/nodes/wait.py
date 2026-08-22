"""A plain, configurable pause -- <Wait seconds="2.5"/> holds RUNNING for
that many seconds then returns SUCCESS. Unlike StubActionNode's fixed
SIMULATED_DELAY_S (0.3s, meant to just prove the pipeline works end to
end), this is a real, reusable BT primitive (matching the "Wait"/"Delay"
vocabulary most BT frameworks ship) with an owner-configurable duration --
useful on its own merits (pacing a tree, rate-limiting a retry loop) and
also the natural way to build a tree that visibly takes real wall-clock
time to tick through, e.g. for watching the live status view.
"""
import time

from py_trees import behaviour, common

from .base import resolve_attrs, BlackboardKeyError


class WaitNode(behaviour.Behaviour):
    TAG = "Wait"

    def __init__(self, name, attrs, blackboard):
        super().__init__(name=name)
        self.attrs = attrs
        self.blackboard = blackboard
        self._started_at = None

    def initialise(self):
        self._started_at = None

    def update(self):
        try:
            resolved = resolve_attrs(self.attrs, self.blackboard)
        except BlackboardKeyError as e:
            self.feedback_message = str(e)
            return common.Status.FAILURE

        try:
            seconds = float(resolved.get("seconds", 1.0))
        except (TypeError, ValueError):
            self.feedback_message = f"seconds={resolved.get('seconds')!r} is not a number"
            return common.Status.FAILURE

        if self._started_at is None:
            self._started_at = time.monotonic()

        if time.monotonic() - self._started_at < seconds:
            return common.Status.RUNNING
        return common.Status.SUCCESS
