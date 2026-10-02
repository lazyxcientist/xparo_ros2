"""BT.CPP v4 built-in nodes -- the set Groot2 shows in its palette -- for
the XPARO engine, so a tree authored in the dashboard (or Groot2) with any
standard node runs here with the same meaning.

Everything registers into node_registry.NODE_REGISTRY under its BT.CPP tag
and follows the registry's builder convention
`(name, attrs, blackboard, children, ros_node) -> Behaviour`. Attributes are
resolved when the node (re)starts, so "{blackboard_key}" works for every
port, exactly like BT.CPP.

Deliberate differences, all documented where they happen:
  * SKIPPED (BT.CPP 4) doesn't exist in py_trees; a skipped node reports
    SUCCESS and says "skipped" in its feedback message.
  * Loops that finish a child instantly (Repeat, RetryUntilSuccessful,
    KeepRunningUntilFailure, Loop*) run up to MAX_SYNC_ITERATIONS rounds
    in one tick and then yield RUNNING -- fast, but an infinite loop can
    never freeze the robot's tick loop.
  * SubTree shares the parent tree's blackboard (BT.CPP's _autoremap="true");
    explicit port attributes are copied in when it starts. See tree_builder.
  * ManualSelector isn't provided: it needs a person at the robot's console.
"""
import re
import time

import py_trees
from py_trees import common

from . import expr
from .nodes.base import _PLACEHOLDER_RE
from .node_registry import register, register_class, NODE_ARITY

S = common.Status
MAX_SYNC_ITERATIONS = 50
_STATUS_BY_NAME = {"SUCCESS": S.SUCCESS, "FAILURE": S.FAILURE, "RUNNING": S.RUNNING, "SKIPPED": S.SUCCESS}


class NodeConfigError(Exception):
    """A node's ports hold a value it can't use (e.g. num_cycles="abc")."""


def attr_value(attrs, blackboard, key, default=None):
    """One port's value: a literal, or blackboard[name] for "{name}"."""
    raw = attrs.get(key)
    if raw is None:
        return default
    match = _PLACEHOLDER_RE.match(raw)
    if match:
        return blackboard.get(match.group(1), default)
    return raw


def output_key(attrs, key):
    """The blackboard key an output port writes to ("{x}" or bare "x")."""
    raw = (attrs.get(key) or "").strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1].strip()
    return raw


def int_value(attrs, blackboard, key, default):
    value = attr_value(attrs, blackboard, key, default)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise NodeConfigError(f"{key}={value!r} is not a whole number")


def float_value(attrs, blackboard, key, default):
    value = attr_value(attrs, blackboard, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise NodeConfigError(f"{key}={value!r} is not a number")


def count_from(value, n_children):
    """BT.CPP counts: a negative value means N + value + 1 (so -1 = all)."""
    return n_children + value + 1 if value < 0 else value


class _PortsMixin:
    """Leaf helper: turns NodeConfigError into FAILURE with a clear message."""

    def _fail(self, message):
        self.feedback_message = message
        return S.FAILURE


# ---------------------------------------------------------------- actions

class AlwaysStatus(py_trees.behaviour.Behaviour):
    def __init__(self, name, status):
        super().__init__(name=name)
        self._status = status

    def update(self):
        return self._status


class SetBlackboard(_PortsMixin, py_trees.behaviour.Behaviour):
    """<SetBlackboard output_key="x" value="1"/> (value may be "{other}")."""

    def __init__(self, name, attrs, blackboard):
        super().__init__(name=name)
        self.attrs, self.blackboard = attrs, blackboard

    def update(self):
        key = output_key(self.attrs, "output_key")
        if not key:
            return self._fail("output_key is empty")
        value = attr_value(self.attrs, self.blackboard, "value", "")
        self.blackboard[key] = value
        self.feedback_message = f"{key} = {value!r}"
        return S.SUCCESS


class UnsetBlackboard(_PortsMixin, py_trees.behaviour.Behaviour):
    def __init__(self, name, attrs, blackboard):
        super().__init__(name=name)
        self.attrs, self.blackboard = attrs, blackboard

    def update(self):
        key = output_key(self.attrs, "key")
        if not key:
            return self._fail("key is empty")
        self.blackboard.pop(key, None)
        return S.SUCCESS


class Sleep(_PortsMixin, py_trees.behaviour.Behaviour):
    """<Sleep msec="500"/> -- RUNNING until the time has passed."""

    def __init__(self, name, attrs, blackboard):
        super().__init__(name=name)
        self.attrs, self.blackboard = attrs, blackboard
        self._until = None

    def initialise(self):
        self._until = None

    def update(self):
        if self._until is None:
            try:
                msec = float_value(self.attrs, self.blackboard, "msec", 0)
            except NodeConfigError as e:
                return self._fail(str(e))
            self._until = time.monotonic() + max(msec, 0) / 1000.0
        return S.RUNNING if time.monotonic() < self._until else S.SUCCESS


class ScriptCondition(_PortsMixin, py_trees.behaviour.Behaviour):
    """<ScriptCondition code="battery > 20"/> -- SUCCESS when true. Unlike
    the forgiving _skipIf family, a broken expression here is a FAILURE
    with the reason, so a typo can't silently look like "false"."""

    def __init__(self, name, attrs, blackboard):
        super().__init__(name=name)
        self.attrs, self.blackboard = attrs, blackboard

    def update(self):
        code = attr_value(self.attrs, self.blackboard, "code", "") or ""
        try:
            result = expr.truthy(expr.evaluate(code, self.blackboard))
        except expr.ExpressionError as e:
            return self._fail(str(e))
        self.feedback_message = f"{code!r} is {result}"
        return S.SUCCESS if result else S.FAILURE


class WasEntryUpdated(_PortsMixin, py_trees.behaviour.Behaviour):
    """SUCCESS if blackboard[entry] changed since this node last checked."""

    def __init__(self, name, attrs, blackboard):
        super().__init__(name=name)
        self.attrs, self.blackboard = attrs, blackboard
        self._seen = None

    def update(self):
        key = output_key(self.attrs, "entry")
        version = entry_version(self.blackboard, key)
        updated = version > 0 if self._seen is None else version != self._seen
        self._seen = version
        return S.SUCCESS if updated else S.FAILURE


def entry_version(blackboard, key):
    versions = getattr(blackboard, "versions", None)
    return versions.get(key, 0) if versions is not None else (1 if key in blackboard else 0)


class TrackedBlackboard(dict):
    """The run's blackboard: a plain dict (so it serializes as one) that
    also counts writes per key, for WasEntryUpdated / SkipUnlessUpdated /
    WaitValueUpdate and for the execution report's "changed keys"."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.versions = {key: 1 for key in self}

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.versions[key] = self.versions.get(key, 0) + 1

    def __delitem__(self, key):
        super().__delitem__(key)
        self.versions[key] = self.versions.get(key, 0) + 1

    def pop(self, key, *default):
        self.versions[key] = self.versions.get(key, 0) + 1
        return super().pop(key, *default)

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value


# ------------------------------------------------------------- decorators

class _Decorator(py_trees.decorators.Decorator):
    """Decorator whose tick() the subclass fully controls via step()."""

    def __init__(self, name, child, attrs, blackboard):
        super().__init__(name=name, child=child)
        self.attrs, self.blackboard = attrs, blackboard

    def update(self):  # unused: tick() is overridden
        return self.status

    def tick(self):
        if self.status != S.RUNNING:
            self.initialise()
            try:
                self.configure()
            except NodeConfigError as e:
                self.feedback_message = str(e)
                self._finish(S.FAILURE)
                yield self
                return
        new_status = yield from self.step()
        self._finish(new_status)
        yield self

    def configure(self):
        """Read ports when (re)starting; raise NodeConfigError if invalid."""

    def step(self):
        raise NotImplementedError
        yield  # pragma: no cover

    def _finish(self, new_status):
        if new_status != S.RUNNING:
            self.stop(new_status)
        self.status = new_status

    def _restart_child(self):
        if self.decorated.status != S.INVALID:
            self.decorated.stop(S.INVALID)


class Repeat(_Decorator):
    """num_cycles times while the child succeeds (-1 = forever)."""

    def configure(self):
        self.cycles = int_value(self.attrs, self.blackboard, "num_cycles", 1)
        self.done = 0

    def step(self):
        for _ in range(MAX_SYNC_ITERATIONS):
            yield from self.decorated.tick()
            child = self.decorated.status
            if child != S.SUCCESS:
                return child  # RUNNING or FAILURE
            self.done += 1
            self.feedback_message = f"cycle {self.done}" + (f" of {self.cycles}" if self.cycles >= 0 else "")
            if 0 <= self.cycles <= self.done:
                return S.SUCCESS
            self._restart_child()
        return S.RUNNING


class RetryUntilSuccessful(_Decorator):
    """Retries a failing child up to num_attempts times (-1 = forever)."""

    def configure(self):
        self.attempts = int_value(self.attrs, self.blackboard, "num_attempts", 1)
        self.failures = 0

    def step(self):
        for _ in range(MAX_SYNC_ITERATIONS):
            yield from self.decorated.tick()
            child = self.decorated.status
            if child != S.FAILURE:
                return child  # RUNNING or SUCCESS
            self.failures += 1
            self.feedback_message = f"attempt {self.failures} failed" + (
                f" (of {self.attempts})" if self.attempts >= 0 else ""
            )
            if 0 <= self.attempts <= self.failures:
                return S.FAILURE
            self._restart_child()
        return S.RUNNING


class KeepRunningUntilFailure(_Decorator):
    def step(self):
        for _ in range(MAX_SYNC_ITERATIONS):
            yield from self.decorated.tick()
            child = self.decorated.status
            if child == S.FAILURE:
                return S.FAILURE
            if child == S.RUNNING:
                return S.RUNNING
            self._restart_child()
        return S.RUNNING


class Delay(_Decorator):
    """Waits delay_msec before ticking its child."""

    def configure(self):
        self.until = time.monotonic() + max(float_value(self.attrs, self.blackboard, "delay_msec", 0), 0) / 1000.0

    def step(self):
        if time.monotonic() < self.until:
            return S.RUNNING
        yield from self.decorated.tick()
        return self.decorated.status


class Timeout(_Decorator):
    """FAILURE if the child is still RUNNING after msec."""

    def configure(self):
        self.msec = float_value(self.attrs, self.blackboard, "msec", 0)
        self.deadline = time.monotonic() + max(self.msec, 0) / 1000.0

    def step(self):
        yield from self.decorated.tick()
        child = self.decorated.status
        if child == S.RUNNING and time.monotonic() >= self.deadline:
            self._restart_child()
            self.feedback_message = f"timed out after {self.msec:g} ms"
            return S.FAILURE
        return child


class RunOnce(_Decorator):
    """Runs the child once; afterwards returns its result again (or SUCCESS
    "skipped" when then_skip is true, BT.CPP's default)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.result = None

    def configure(self):
        self.then_skip = str(attr_value(self.attrs, self.blackboard, "then_skip", "true")).lower() != "false"

    def step(self):
        if self.result is not None:
            self.feedback_message = "skipped (already ran once)" if self.then_skip else "already ran once"
            return S.SUCCESS if self.then_skip else self.result
        yield from self.decorated.tick()
        child = self.decorated.status
        if child != S.RUNNING:
            self.result = child
        return child


class Precondition(_Decorator):
    """Ticks the child only if `if` is true; otherwise returns `else`."""

    def configure(self):
        self.check = attr_value(self.attrs, self.blackboard, "if", "") or ""
        otherwise = str(attr_value(self.attrs, self.blackboard, "else", "FAILURE")).upper()
        if otherwise not in _STATUS_BY_NAME:
            raise NodeConfigError(f"else={otherwise!r} must be SUCCESS, FAILURE, RUNNING or SKIPPED")
        self.otherwise = otherwise
        try:
            self.allowed = expr.truthy(expr.evaluate(self.check, self.blackboard)) if self.check.strip() else True
        except expr.ExpressionError as e:
            raise NodeConfigError(f"if={self.check!r}: {e}")

    def step(self):
        if not self.allowed:
            self.feedback_message = f"condition {self.check!r} is false, returning {self.otherwise}"
            return _STATUS_BY_NAME[self.otherwise]
        yield from self.decorated.tick()
        return self.decorated.status


class SkipUnlessUpdated(_Decorator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen = 0

    def configure(self):
        key = output_key(self.attrs, "entry")
        version = entry_version(self.blackboard, key)
        self.run_child = version != self._seen
        self._seen = version

    def step(self):
        if not self.run_child:
            self.feedback_message = "skipped (entry not updated)"
            return S.SUCCESS
        yield from self.decorated.tick()
        return self.decorated.status


class WaitValueUpdate(_Decorator):
    def configure(self):
        self.key = output_key(self.attrs, "entry")
        self.start_version = entry_version(self.blackboard, self.key)
        self.updated = False

    def step(self):
        if not self.updated:
            if entry_version(self.blackboard, self.key) == self.start_version:
                self.feedback_message = f"waiting for {self.key!r} to change"
                return S.RUNNING
            self.updated = True
        yield from self.decorated.tick()
        return self.decorated.status


_LOOP_CASTS = {"LoopInt": lambda v: int(float(v)), "LoopDouble": float, "LoopBool": expr.truthy, "LoopString": str}


class Loop(_Decorator):
    """LoopInt/Double/Bool/String: runs the child once per item of `queue`
    (a list, or "a;b;c"), writing the item to the `value` output port.
    The queue is copied, not consumed from the blackboard."""

    def __init__(self, name, child, attrs, blackboard, cast):
        super().__init__(name, child, attrs, blackboard)
        self.cast = cast

    def configure(self):
        raw = attr_value(self.attrs, self.blackboard, "queue", [])
        items = raw if isinstance(raw, (list, tuple)) else [p for p in str(raw).split(";") if p.strip() != ""]
        try:
            self.queue = [self.cast(item.strip() if isinstance(item, str) else item) for item in items]
        except (TypeError, ValueError) as e:
            raise NodeConfigError(f"queue item can't be converted: {e}")
        if_empty = str(attr_value(self.attrs, self.blackboard, "if_empty", "SUCCESS")).upper()
        self.if_empty = _STATUS_BY_NAME.get(if_empty, S.SUCCESS)
        self.started = False

    def step(self):
        for _ in range(MAX_SYNC_ITERATIONS):
            if self.decorated.status != S.RUNNING:
                if not self.queue:
                    return self.if_empty if not self.started else S.SUCCESS
                item = self.queue.pop(0)
                self.started = True
                key = output_key(self.attrs, "value")
                if key:
                    self.blackboard[key] = item
                self.feedback_message = f"item {item!r}, {len(self.queue)} left"
                self._restart_child()
            yield from self.decorated.tick()
            child = self.decorated.status
            if child != S.SUCCESS:
                return child
        return S.RUNNING


# ---------------------------------------------------------------- controls

class _Control(py_trees.composites.Composite):
    def __init__(self, name, children, attrs, blackboard):
        super().__init__(name=name, children=children)
        self.attrs, self.blackboard = attrs, blackboard

    def _finish(self, new_status):
        if new_status != S.RUNNING:
            self.stop(new_status)
        self.status = new_status

    def _halt(self, child):
        if child is not None and child.status == S.RUNNING:
            child.stop(S.INVALID)


class SequenceWithMemory(_Control):
    """Like Sequence, but after a FAILURE it resumes from the failed child
    next time instead of starting over."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index = 0

    def tick(self):
        if self.status != S.RUNNING:
            self.initialise()
        while self.index < len(self.children):
            child = self.children[self.index]
            self.current_child = child
            yield from child.tick()
            if child.status == S.RUNNING:
                self.status = S.RUNNING
                yield self
                return
            if child.status == S.FAILURE:
                self._finish(S.FAILURE)
                yield self
                return
            self.index += 1
        self.index = 0
        self._finish(S.SUCCESS)
        yield self

    def stop(self, new_status=S.INVALID):
        if new_status == S.INVALID:
            self.index = 0
        super().stop(new_status)


class ParallelAll(_Control):
    """Runs every child to completion; FAILURE if at least max_failures
    failed (-1 = all), else SUCCESS."""

    def tick(self):
        if self.status != S.RUNNING:
            self.initialise()
            for child in self.children:
                if child.status != S.INVALID:
                    child.stop(S.INVALID)
            try:
                self.max_failures = count_from(int_value(self.attrs, self.blackboard, "max_failures", 1), len(self.children))
            except NodeConfigError as e:
                self.feedback_message = str(e)
                self._finish(S.FAILURE)
                yield self
                return
        for child in self.children:
            if child.status in (S.SUCCESS, S.FAILURE):
                continue
            yield from child.tick()
        if any(c.status == S.RUNNING for c in self.children):
            self.status = S.RUNNING
            yield self
            return
        failures = sum(1 for c in self.children if c.status == S.FAILURE)
        self.feedback_message = f"{failures} of {len(self.children)} failed"
        self._finish(S.FAILURE if failures >= max(self.max_failures, 1) else S.SUCCESS)
        yield self


class IfThenElse(_Control):
    """Child 1 is the condition; child 2 runs if it succeeds, child 3 (if
    present) if it fails. Returns the branch's result."""

    def tick(self):
        if self.status != S.RUNNING:
            self.initialise()
            self.branch = None
        if self.branch is None:
            condition = self.children[0]
            yield from condition.tick()
            if condition.status == S.RUNNING:
                self.status = S.RUNNING
                yield self
                return
            if condition.status == S.SUCCESS:
                self.branch = 1
            elif len(self.children) == 3:
                self.branch = 2
            else:
                self.feedback_message = "condition failed and there is no else branch"
                self._finish(S.FAILURE)
                yield self
                return
        child = self.children[self.branch]
        self.current_child = child
        yield from child.tick()
        self._finish(child.status)
        yield self


class WhileDoElse(_Control):
    """Reactive IfThenElse: the condition (child 1) is checked every tick;
    switching branch halts the other one."""

    def tick(self):
        if self.status != S.RUNNING:
            self.initialise()
        condition = self.children[0]
        yield from condition.tick()
        if condition.status == S.RUNNING:
            self.status = S.RUNNING
            yield self
            return
        if condition.status == S.SUCCESS:
            active, other = self.children[1], (self.children[2] if len(self.children) == 3 else None)
        elif len(self.children) == 3:
            active, other = self.children[2], self.children[1]
        else:
            self._halt(self.children[1])
            self._finish(S.FAILURE)
            yield self
            return
        self._halt(other)
        self.current_child = active
        yield from active.tick()
        self._finish(active.status)
        yield self


def _same_value(a, b):
    if a is None or b is None:
        return False
    if str(a).strip() == str(b).strip():
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


class Switch(_Control):
    """SwitchN: runs the child whose case_i matches `variable`, else the
    last child (the default). Changing case halts the previous child."""

    def __init__(self, name, children, attrs, blackboard, cases):
        super().__init__(name, children, attrs, blackboard)
        self.cases = cases
        self.running_index = None

    def tick(self):
        if self.status != S.RUNNING:
            self.initialise()
            self.running_index = None
        value = attr_value(self.attrs, self.blackboard, "variable")
        index = self.cases  # default branch
        for i in range(self.cases):
            if _same_value(value, attr_value(self.attrs, self.blackboard, f"case_{i + 1}")):
                index = i
                break
        if self.running_index is not None and self.running_index != index:
            self._halt(self.children[self.running_index])
        self.running_index = index
        child = self.children[index]
        self.current_child = child
        self.feedback_message = f"variable={value!r} -> " + (f"case_{index + 1}" if index < self.cases else "default")
        yield from child.tick()
        self._finish(child.status)
        yield self


# --------------------------------------------------------------- registry

@register("AlwaysSuccess")
def _always_success(name, attrs, blackboard, children, ros_node):
    return AlwaysStatus(name, S.SUCCESS)


@register("AlwaysFailure")
def _always_failure(name, attrs, blackboard, children, ros_node):
    return AlwaysStatus(name, S.FAILURE)


for _tag, _cls in (("SetBlackboard", SetBlackboard), ("UnsetBlackboard", UnsetBlackboard), ("Sleep", Sleep),
                   ("ScriptCondition", ScriptCondition), ("WasEntryUpdated", WasEntryUpdated)):
    register(_tag)(lambda name, attrs, blackboard, children, ros_node, _cls=_cls: _cls(name, attrs, blackboard))

register("Inverter")(lambda name, attrs, blackboard, children, ros_node: py_trees.decorators.Inverter(name=name, child=children[0]))
register("ForceSuccess")(lambda name, attrs, blackboard, children, ros_node: py_trees.decorators.FailureIsSuccess(name=name, child=children[0]))
register("ForceFailure")(lambda name, attrs, blackboard, children, ros_node: py_trees.decorators.SuccessIsFailure(name=name, child=children[0]))

for _tag, _cls in (("Repeat", Repeat), ("RetryUntilSuccessful", RetryUntilSuccessful),
                   ("KeepRunningUntilFailure", KeepRunningUntilFailure), ("Delay", Delay), ("Timeout", Timeout),
                   ("RunOnce", RunOnce), ("Precondition", Precondition), ("SkipUnlessUpdated", SkipUnlessUpdated),
                   ("WaitValueUpdate", WaitValueUpdate)):
    register(_tag)(lambda name, attrs, blackboard, children, ros_node, _cls=_cls: _cls(name, children[0], attrs, blackboard))

for _tag, _cast in _LOOP_CASTS.items():
    register(_tag)(lambda name, attrs, blackboard, children, ros_node, _cast=_cast: Loop(name, children[0], attrs, blackboard, _cast))

# BT.CPP's Async variants differ only in yielding between children; within
# this engine's tick loop they behave exactly like the memory versions.
register_class("AsyncSequence", py_trees.composites.Sequence, memory=True)
register_class("AsyncFallback", py_trees.composites.Selector, memory=True)
# BT.CPP v3 name for SequenceWithMemory, still found in older trees.
register("SequenceWithMemory")(lambda name, attrs, blackboard, children, ros_node: SequenceWithMemory(name, children, attrs, blackboard))
register("SequenceStar")(lambda name, attrs, blackboard, children, ros_node: SequenceWithMemory(name, children, attrs, blackboard))
register("ParallelAll")(lambda name, attrs, blackboard, children, ros_node: ParallelAll(name, children, attrs, blackboard))
register("IfThenElse")(lambda name, attrs, blackboard, children, ros_node: IfThenElse(name, children, attrs, blackboard))
register("WhileDoElse")(lambda name, attrs, blackboard, children, ros_node: WhileDoElse(name, children, attrs, blackboard))
for _n in range(2, 7):
    register(f"Switch{_n}")(lambda name, attrs, blackboard, children, ros_node, _n=_n: Switch(name, children, attrs, blackboard, _n))

# How many children each built-in accepts: (min, max); None = no limit.
# Used to reject a malformed tree with a clear message before it runs.
_LEAVES = ("AlwaysSuccess", "AlwaysFailure", "SetBlackboard", "UnsetBlackboard", "Sleep", "ScriptCondition",
           "WasEntryUpdated", "Script", "Wait", "ParamSet", "PlayAudio", "DockRobot", "CheckBatteryLevel",
           "SpeakText", "NotifyPatient", "LoadNextDelivery", "NavigateTo")
_DECORATORS = ("Inverter", "ForceSuccess", "ForceFailure", "Repeat", "RetryUntilSuccessful", "KeepRunningUntilFailure",
               "Delay", "Timeout", "RunOnce", "Precondition", "SkipUnlessUpdated", "WaitValueUpdate",
               "LoopInt", "LoopDouble", "LoopBool", "LoopString")
NODE_ARITY.update({tag: (0, 0) for tag in _LEAVES})
NODE_ARITY.update({tag: (1, 1) for tag in _DECORATORS})
NODE_ARITY.update({"IfThenElse": (2, 3), "WhileDoElse": (2, 3)})
NODE_ARITY.update({f"Switch{n}": (n + 1, n + 1) for n in range(2, 7)})
for _tag in ("Sequence", "Fallback", "Selector", "ReactiveSequence", "ReactiveFallback", "Parallel", "ParallelAll",
             "SequenceWithMemory", "SequenceStar", "AsyncSequence", "AsyncFallback"):
    # 0, not 1: an empty control is harmless (it just succeeds) and older
    # trees have them, so it's a warning (tree_builder), not an error.
    NODE_ARITY[_tag] = (0, None)

# Tags that exist in BT.CPP but can't run here, with the reason shown to
# the user instead of a bare "unknown node".
UNSUPPORTED_REASONS = {
    "ManualSelector": "it asks a person at the robot's console to pick a child, which a robot running a task can't do",
}
