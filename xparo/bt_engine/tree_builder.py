"""Behaviour Tree redesign Phase 9: ET.Element -> py_trees.behaviour.Behaviour,
recursively. Every node (leaf or composite) gets wrapped in a
ConditionalDecorator when it carries any of BT.CPP's _skipIf/_successIf/
_failureIf/_while attributes, so that logic lives exactly once regardless
of which registry entry built the node underneath.
"""
import py_trees
from py_trees import common

from . import expr
from .node_registry import NODE_REGISTRY
from .xml_parser import parse_fragment, single_root_child, TreeParseError

_CONDITIONAL_ATTRS = ("_skipIf", "_successIf", "_failureIf", "_while")


class UnknownNodeError(Exception):
    pass


class ConditionalDecorator(py_trees.decorators.Decorator):
    """Evaluated fresh on every tick (not once at build time) -- the
    blackboard values these conditions read can change between ticks, e.g.
    a Script node earlier in the same Sequence assigning a flag a later
    sibling's _skipIf reads. Priority when several are present on one node
    (not seen in this repo's real trees, but the attributes aren't
    mutually exclusive in BT.CPP): skip, then failure, then success, then
    while -- skip is the most unconditional "pretend this node isn't here"
    of the four, so it wins if more than one somehow applies at once.
    """

    def __init__(self, name, child, blackboard, skip_if=None, success_if=None, failure_if=None, while_cond=None):
        super().__init__(name=name, child=child)
        self.blackboard = blackboard
        self.skip_if = skip_if
        self.success_if = success_if
        self.failure_if = failure_if
        self.while_cond = while_cond

    def tick(self):
        if self.skip_if and expr.evaluate_condition(self.skip_if, self.blackboard):
            self._short_circuit(common.Status.SUCCESS)
            yield self
            return
        if self.failure_if and expr.evaluate_condition(self.failure_if, self.blackboard):
            self._short_circuit(common.Status.FAILURE)
            yield self
            return
        if self.success_if and expr.evaluate_condition(self.success_if, self.blackboard):
            self._short_circuit(common.Status.SUCCESS)
            yield self
            return
        if self.while_cond and not expr.evaluate_condition(self.while_cond, self.blackboard):
            self._short_circuit(common.Status.FAILURE)
            yield self
            return
        yield from super().tick()

    def update(self):
        # Never actually reached -- tick() above is fully overridden and
        # never calls this, but Behaviour is an ABC that requires a
        # concrete update() to even instantiate the class.
        return self.decorated.status

    def _short_circuit(self, new_status):
        # The child is deliberately never ticked here -- _successIf/
        # _failureIf force a result without running it at all, and
        # _skipIf's whole point is to pretend the node isn't there.
        if self.decorated.status != common.Status.INVALID:
            self.decorated.stop(common.Status.INVALID)
        self.stop(new_status)
        self.status = new_status


def _wrap_conditional(node, attrs, blackboard):
    if not any(attrs.get(a) for a in _CONDITIONAL_ATTRS):
        return node
    return ConditionalDecorator(
        name=f"{node.name} (conditional)",
        child=node,
        blackboard=blackboard,
        skip_if=attrs.get("_skipIf"),
        success_if=attrs.get("_successIf"),
        failure_if=attrs.get("_failureIf"),
        while_cond=attrs.get("_while"),
    )


def build_node(element, blackboard, ros_node=None):
    """Recursively builds one ET.Element (and its children) into a
    py_trees.behaviour.Behaviour tree. `ros_node` is the live rclpy Node
    hosting this tree (None when ticking offline, as every test in this
    repo does) -- threaded through to every builder call so leaf nodes
    with real ROS interfaces to call (Phase 10's ParamSet is the first)
    can create clients/publishers on it. Composite/control-flow builders
    ignore it; Phase 9 didn't need this parameter at all until Phase 10
    introduced the first leaf that does.
    """
    tag = element.tag
    builder = NODE_REGISTRY.get(tag)
    if builder is None:
        raise UnknownNodeError(f"no registered node for tag <{tag}>")

    children = [build_node(child_el, blackboard, ros_node) for child_el in element]
    node = builder(tag, element.attrib, blackboard, children, ros_node)
    return _wrap_conditional(node, element.attrib, blackboard)


def build_tree(xml_fragment, blackboard, ros_node=None):
    """Parses a BT XML fragment and builds a py_trees tree from it.
    `blackboard` is a plain dict, shared by reference with every node in
    the tree (see executor.py's module docstring for why this engine uses
    a plain dict instead of py_trees' own global Blackboard/Client system).
    Returns the root py_trees.behaviour.Behaviour.
    """
    root_element = single_root_child(parse_fragment(xml_fragment))
    return build_node(root_element, blackboard, ros_node)
