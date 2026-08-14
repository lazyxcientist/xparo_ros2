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
