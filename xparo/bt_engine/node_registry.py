"""Behaviour Tree redesign Phase 9: tag -> builder registry. tree_builder.py
resolves every XML element's tag through this before building it into a
py_trees node. Phase 12/13's plugin loader adds to the same registry (by
XML_TAG) rather than needing its own separate lookup mechanism.

Builder calling convention: every entry is a callable
`(name, attrs, blackboard, children) -> py_trees.behaviour.Behaviour`,
where `attrs` is the element's raw (unresolved) attribute dict and
`children` is the list of already-built py_trees children (empty for
leaves). Leaf node builders (Phase 10) resolve their own "{name}"
attributes against blackboard at tick time, not here at build time --
blackboard values can change between ticks (e.g. a Script node earlier in
a Sequence assigning a value a later sibling reads).
"""
import py_trees

NODE_REGISTRY = {}


def register(tag):
    def decorator(builder):
        NODE_REGISTRY[tag] = builder
        return builder
    return decorator


def register_class(tag, cls):
    """For plain py_trees classes that don't need attrs/blackboard at all
    (Sequence, Selector) -- wraps them in the same calling convention."""
    NODE_REGISTRY[tag] = lambda name, attrs, blackboard, children, cls=cls: cls(
        name=name, memory=False, children=children
    )


register_class("Sequence", py_trees.composites.Sequence)
register_class("Fallback", py_trees.composites.Selector)
# BT.CPP also accepts py_trees' own name for the same node -- both tags
# build the exact same composite, so a tree authored either way behaves
# identically.
register_class("Selector", py_trees.composites.Selector)


@register("Parallel")
def _build_parallel(name, attrs, blackboard, children):
    from .composites import CountingParallel
    success_count = int(attrs.get("success_count", len(children)))
    failure_count = int(attrs.get("failure_count", 1))
    return CountingParallel(name=name, success_count=success_count, failure_count=failure_count, children=children)


@register("RetryUntilSuccessful")
def _build_retry(name, attrs, blackboard, children):
    if len(children) != 1:
        raise ValueError(f"RetryUntilSuccessful {name!r} needs exactly one child, got {len(children)}")
    num_attempts = int(attrs.get("num_attempts", 1))
    # py_trees 2.5 already ships this exact "retry N times, then propagate
    # FAILURE; SUCCESS short-circuits immediately" behaviour as
    # decorators.Retry -- confirmed by reading its source, not assumed.
    # Only the tag name differs from BT.CPP's; no need to hand-roll one.
    return py_trees.decorators.Retry(name=name, child=children[0], num_failures=num_attempts)


@register("Script")
def _build_script(name, attrs, blackboard, children):
    from .nodes.script import ScriptNode
    return ScriptNode(name=name, code=attrs.get("code", ""), blackboard=blackboard)
