"""Behaviour Tree redesign Phase 9: tag -> builder registry. tree_builder.py
resolves every XML element's tag through this before building it into a
py_trees node. Phase 12/13's plugin loader adds to the same registry (by
XML_TAG) rather than needing its own separate lookup mechanism.

Builder calling convention: every entry is a callable
`(name, attrs, blackboard, children, ros_node) -> py_trees.behaviour.Behaviour`,
where `attrs` is the element's raw (unresolved) attribute dict, `children`
is the list of already-built py_trees children (empty for leaves), and
`ros_node` is the live rclpy Node hosting this tree (None when ticking
offline, as every test does) -- only Phase 10's ParamSet actually uses it
so far. Leaf node builders resolve their own "{name}" attributes against
blackboard at tick time, not here at build time -- blackboard values can
change between ticks (e.g. a Script node earlier in a Sequence assigning a
value a later sibling reads).
"""
import py_trees

NODE_REGISTRY = {}


def register(tag):
    def decorator(builder):
        NODE_REGISTRY[tag] = builder
        return builder
    return decorator


def register_class(tag, cls, memory):
    """For plain py_trees classes that don't need attrs/blackboard/ros_node
    at all (Sequence, Selector) -- wraps them in the same calling
    convention."""
    NODE_REGISTRY[tag] = lambda name, attrs, blackboard, children, ros_node, cls=cls, memory=memory: cls(
        name=name, memory=memory, children=children
    )


# BTCPP_format="4" (every tree in this repo declares this) renamed BT.CPP's
# node vocabulary from v3: plain <Sequence>/<Fallback> now mean *with*
# memory (skip already-succeeded children, resume from whichever one is
# RUNNING) -- <ReactiveSequence>/<ReactiveFallback> are the *without*-
# memory variants (always re-tick from the first child). Confirmed the
# hard way, not just read about it: an earlier memory=False mapping here
# made quick_delivery_tree.xml loop forever between PlayAudio and
# CheckBatteryLevel, restarting PlayAudio's already-succeeded stub from
# scratch on every tick -- without memory, a Sequence resets to child 0
# every tick it's re-entered while RUNNING, which for a chain of one-shot
# actions (not reactive conditions) never converges past the first one
# that goes RUNNING at all.
register_class("Sequence", py_trees.composites.Sequence, memory=True)
register_class("Fallback", py_trees.composites.Selector, memory=True)
# BT.CPP also accepts py_trees' own name for the same node -- both tags
# build the exact same composite, so a tree authored either way behaves
# identically.
register_class("Selector", py_trees.composites.Selector, memory=True)
register_class("ReactiveSequence", py_trees.composites.Sequence, memory=False)
register_class("ReactiveFallback", py_trees.composites.Selector, memory=False)


@register("Parallel")
def _build_parallel(name, attrs, blackboard, children, ros_node):
    from .composites import CountingParallel
    success_count = int(attrs.get("success_count", len(children)))
    failure_count = int(attrs.get("failure_count", 1))
    return CountingParallel(name=name, success_count=success_count, failure_count=failure_count, children=children)


@register("RetryUntilSuccessful")
def _build_retry(name, attrs, blackboard, children, ros_node):
    if len(children) != 1:
        raise ValueError(f"RetryUntilSuccessful {name!r} needs exactly one child, got {len(children)}")
    num_attempts = int(attrs.get("num_attempts", 1))
    # py_trees 2.5 already ships this exact "retry N times, then propagate
    # FAILURE; SUCCESS short-circuits immediately" behaviour as
    # decorators.Retry -- confirmed by reading its source, not assumed.
    # Only the tag name differs from BT.CPP's; no need to hand-roll one.
    return py_trees.decorators.Retry(name=name, child=children[0], num_failures=num_attempts)


@register("Script")
def _build_script(name, attrs, blackboard, children, ros_node):
    from .nodes.script import ScriptNode
    return ScriptNode(name=name, code=attrs.get("code", ""), blackboard=blackboard)


@register("ParamSet")
def _build_param_set(name, attrs, blackboard, children, ros_node):
    from .nodes.param_set import ParamSetNode
    return ParamSetNode(name=name, attrs=attrs, blackboard=blackboard, ros_node=ros_node)


@register("Wait")
def _build_wait(name, attrs, blackboard, children, ros_node):
    from .nodes.wait import WaitNode
    return WaitNode(name=name, attrs=attrs, blackboard=blackboard)


def _register_stub(tag, module_name, class_name):
    @register(tag)
    def _builder(name, attrs, blackboard, children, ros_node, module_name=module_name, class_name=class_name):
        import importlib
        module = importlib.import_module(f".nodes.{module_name}", package=__package__)
        cls = getattr(module, class_name)
        return cls(name=name, attrs=attrs, blackboard=blackboard)
    return _builder


_register_stub("PlayAudio", "play_audio", "PlayAudioNode")
_register_stub("DockRobot", "dock_robot", "DockRobotNode")
_register_stub("CheckBatteryLevel", "check_battery_level", "CheckBatteryLevelNode")
_register_stub("SpeakText", "speak_text", "SpeakTextNode")
_register_stub("NotifyPatient", "notify_patient", "NotifyPatientNode")
_register_stub("LoadNextDelivery", "load_next_delivery", "LoadNextDeliveryNode")
_register_stub("NavigateTo", "navigate_to", "NavigateToNode")
