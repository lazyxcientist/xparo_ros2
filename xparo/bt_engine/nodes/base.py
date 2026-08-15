"""Behaviour Tree redesign Phase 9/10: shared helpers for leaf node
implementations. A leaf's raw XML attributes may be literal values or
"{blackboard_key}" substitutions (BT.CPP's port-remapping syntax) --
resolve_attrs() resolves the latter against the shared blackboard dict.
Resolution happens at tick time (inside a node's own update(), calling
this fresh every tick), not once at build time in tree_builder.py --
blackboard values can change between ticks, e.g. a Script node earlier in
the same Sequence assigning a value a later sibling reads.
"""
import re
import time

import py_trees
from py_trees import common

_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class BlackboardKeyError(Exception):
    pass


def resolve_attrs(raw_attrs, blackboard, required=()):
    """Returns a new dict with every "{name}" value replaced by
    blackboard[name]; literal values pass through unchanged. `required`
    lists attribute names that must resolve to something other than None
    (missing entirely, or a "{name}" pointing at an unset blackboard key)
    -- raises BlackboardKeyError naming exactly which one, rather than a
    leaf node discovering a None deep inside its own logic.
    """
    resolved = {}
    for key, raw_value in raw_attrs.items():
        match = _PLACEHOLDER_RE.match(raw_value)
        if match:
            resolved[key] = blackboard.get(match.group(1))
        else:
            resolved[key] = raw_value

    for key in required:
        if resolved.get(key) is None:
            raise BlackboardKeyError(f"{key!r} is required but resolved to None (raw: {raw_attrs.get(key)!r})")
    return resolved


class StubActionNode(py_trees.behaviour.Behaviour):
    """Honest-scoping base for every leaf tag this repo has zero real
    subsystem to hook into yet (confirmed by grep at planning time: no
    audio/TTS/docking/battery-telemetry/Nav2-action-client infrastructure
    anywhere in this repo, only a passive BehaviorTreeLog *subscription*).
    Resolves its attributes, logs them, holds RUNNING for
    SIMULATED_DELAY_S, then returns SUCCESS -- so the full parse->build->
    resolve->tick->relay pipeline is genuinely exercisable end-to-end
    today, while real hardware wiring is called out explicitly as
    follow-up work rather than silently faked as "done". Subclasses set
    TAG (for logging) and REQUIRED_ATTRS (passed to resolve_attrs).
    """

    TAG = "StubAction"
    REQUIRED_ATTRS = ()
    SIMULATED_DELAY_S = 0.3

    def __init__(self, name, attrs, blackboard):
        super().__init__(name=name)
        self.attrs = attrs
        self.blackboard = blackboard
        self._started_at = None

    def initialise(self):
        self._started_at = None

    def update(self):
        try:
            resolved = resolve_attrs(self.attrs, self.blackboard, required=self.REQUIRED_ATTRS)
        except BlackboardKeyError as e:
            self.feedback_message = str(e)
            return common.Status.FAILURE

        if self._started_at is None:
            self._started_at = time.monotonic()
            self.feedback_message = f"[STUB] {self.TAG}: {resolved}"
            print(f"[bt_engine STUB] {self.TAG} ({self.name}): {resolved}")

        if time.monotonic() - self._started_at < self.SIMULATED_DELAY_S:
            return common.Status.RUNNING
        return common.Status.SUCCESS
