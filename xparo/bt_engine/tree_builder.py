"""Behaviour Tree redesign Phase 9: ET.Element -> py_trees.behaviour.Behaviour,
recursively. Every node (leaf or composite) gets wrapped in a
ConditionalDecorator when it carries any of BT.CPP's _skipIf/_successIf/
_failureIf/_while attributes, so that logic lives exactly once regardless
of which registry entry built the node underneath.

Also:
  * validate_tree() checks a whole tree *before* it runs and lists every
    problem it finds (unknown node, wrong number of children, missing
    SubTree, unreadable condition), each with the path to the node, so a
    task fails with one clear message instead of half-running.
  * <SubTree ID="other_tree"/> expands another behaviour tree in place.
  * A full BT.CPP / Groot2 document (<root><BehaviorTree ID=...>...) is
    accepted too: its main tree runs, its other trees serve as subtrees.
"""
import py_trees
from py_trees import common

from . import expr
from .node_registry import NODE_REGISTRY, NODE_ARITY
from .xml_parser import parse_fragment, single_root_child, TreeParseError

_CONDITIONAL_ATTRS = ("_skipIf", "_successIf", "_failureIf", "_while")
MAX_SUBTREE_DEPTH = 16
# Finding F10 (LOW): validate_tree had no cap on a single tree's own size
# or plain XML nesting depth (distinct from MAX_SUBTREE_DEPTH just above,
# which only bounds <SubTree> recursion) -- a RUN_TASK dispatch with an
# enormous or pathologically deep tree_xml would be accepted and handed
# straight to build_tree/the tick loop with no limit, tying up the robot
# (or its Python recursion limit) on a single task. These are deliberately
# generous -- comfortably above MAX_REPORT_NODES (500, executor.py) and
# any real hand-authored tree -- and only exist to give a clear rejection
# instead of an unbounded build.
MAX_TREE_NODES = 4000
MAX_TREE_DEPTH = 200


class TreeBuildError(Exception):
    """A node couldn't be built. `path` says where it is in the tree."""

    def __init__(self, message, path="", tag=""):
        super().__init__(f"{message} (at {path})" if path else message)
        self.reason = message
        self.path = path
        self.tag = tag


class UnknownNodeError(TreeBuildError):
    pass


class TreeValidationError(TreeBuildError):
    """validate_tree found errors; `.problems` lists all of them."""

    def __init__(self, problems):
        errors = [p for p in problems if p["level"] == "error"]
        first = errors[0] if errors else {"message": "invalid tree", "path": ""}
        more = f" (+{len(errors) - 1} more problem{'s' if len(errors) > 2 else ''})" if len(errors) > 1 else ""
        super().__init__(first["message"] + more, first.get("path", ""), first.get("tag", ""))
        self.problems = problems


class ConditionalDecorator(py_trees.decorators.Decorator):
    """Evaluated fresh on every tick (not once at build time) -- the
    blackboard values these conditions read can change between ticks, e.g.
    a Script node earlier in the same Sequence assigning a flag a later
    sibling's _skipIf reads. Priority when several are present on one node
    (not seen in this repo's real trees, but the attributes aren't
    mutually exclusive in BT.CPP): skip, then failure, then success, then
    while -- skip is the most unconditional "pretend this node isn't here"
    of the four, so it wins if more than one somehow applies at once.

    A condition that can't be evaluated (a variable nobody set yet, or a
    typo) counts as false, as before -- but the reason is kept in
    feedback_message so the task's execution report shows it.
    """

    def __init__(self, name, child, blackboard, skip_if=None, success_if=None, failure_if=None, while_cond=None):
        super().__init__(name=name, child=child)
        self.blackboard = blackboard
        self.skip_if = skip_if
        self.success_if = success_if
        self.failure_if = failure_if
        self.while_cond = while_cond
        self.xparo_synthetic = True
        # What the last tick decided: "skip", "failure", "success",
        # "while" (short-circuits) or "run" (the node itself ran).
        self.last_gate = None

    def conditions(self):
        pairs = (("_skipIf", self.skip_if), ("_failureIf", self.failure_if),
                 ("_successIf", self.success_if), ("_while", self.while_cond))
        return {k: v for k, v in pairs if v}

    def _check(self, attr, expression):
        try:
            return expr.truthy(expr.evaluate(expression, self.blackboard)), ""
        except expr.ExpressionError as e:
            return False, f"{attr}={expression!r} could not be evaluated ({e}), treated as false"

    def tick(self):
        notes = []
        for attr, expression, gate, status in (
            ("_skipIf", self.skip_if, "skip", common.Status.SUCCESS),
            ("_failureIf", self.failure_if, "failure", common.Status.FAILURE),
            ("_successIf", self.success_if, "success", common.Status.SUCCESS),
        ):
            if not expression:
                continue
            value, note = self._check(attr, expression)
            if note:
                notes.append(note)
            if value:
                self.last_gate = gate
                verb = "skipped" if gate == "skip" else f"forced {status.name}"
                self.feedback_message = f"{verb}: {attr} {expression!r} was true"
                self._short_circuit(status)
                yield self
                return
        if self.while_cond:
            value, note = self._check("_while", self.while_cond)
            if note:
                notes.append(note)
            if not value:
                self.last_gate = "while"
                self.feedback_message = f"stopped: _while {self.while_cond!r} was false" + (f" -- {note}" if note else "")
                self._short_circuit(common.Status.FAILURE)
                yield self
                return
        self.last_gate = "run"
        self.feedback_message = "; ".join(notes)
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


class SubTreeNode(py_trees.decorators.Decorator):
    """<SubTree ID="deliver" target="{room}"/>: runs behaviour tree
    "deliver" in place. The subtree shares this tree's blackboard (BT.CPP's
    _autoremap="true"), so it sees every variable. Port attributes are
    copied in when it starts -- target="{room}" sets the subtree's
    `target` to this tree's `room`, target="kitchen" sets it to the text --
    and "{name}" ports are copied back out when it finishes."""

    def __init__(self, name, child, attrs, blackboard, tree_id):
        super().__init__(name=name, child=child)
        self.attrs = attrs
        self.blackboard = blackboard
        self.tree_id = tree_id

    def _ports(self):
        for key, raw in self.attrs.items():
            if key in ("ID", "name", "__shared_blackboard") or key.startswith("_"):
                continue
            yield key, raw

    def initialise(self):
        for key, raw in self._ports():
            match = _placeholder(raw)
            if match is None:
                self.blackboard[key] = raw
            elif match not in ("=", key):
                self.blackboard[key] = self.blackboard.get(match)

    def update(self):
        return self.decorated.status

    def terminate(self, new_status):
        if new_status not in (common.Status.SUCCESS, common.Status.FAILURE):
            return
        for key, raw in self._ports():
            match = _placeholder(raw)
            if match and match not in ("=", key) and key in self.blackboard:
                self.blackboard[match] = self.blackboard[key]


def _placeholder(raw):
    raw = (raw or "").strip()
    if len(raw) > 2 and raw.startswith("{") and raw.endswith("}"):
        return raw[1:-1].strip()
    return None


def _wrap_conditional(node, attrs, blackboard):
    if not any(attrs.get(a) for a in _CONDITIONAL_ATTRS):
        return node
    wrapper = ConditionalDecorator(
        name=f"{node.name} (conditional)",
        child=node,
        blackboard=blackboard,
        skip_if=attrs.get("_skipIf"),
        success_if=attrs.get("_successIf"),
        failure_if=attrs.get("_failureIf"),
        while_cond=attrs.get("_while"),
    )
    # The wrapper is a synthetic node this engine invents (not a real
    # BT.CPP decorator tag) -- reports the same registration tag as the
    # node it wraps, matching how its .name already reads as "that node,
    # conditionally gated" rather than a distinct type of its own.
    wrapper.xparo_tag = getattr(node, "xparo_tag", node.name)
    return wrapper


class BuildContext:
    """What building one tree needs beyond the XML itself: the ROS node,
    where to find subtrees, and which subtrees are being expanded right now
    (to catch a tree that includes itself)."""

    def __init__(self, ros_node=None, subtrees=None, subtree_resolver=None):
        self.ros_node = ros_node
        self.subtrees = dict(subtrees or {})
        self.subtree_resolver = subtree_resolver
        self.stack = []

    def find_subtree(self, tree_id):
        """Returns the subtree's root ET.Element, or raises TreeBuildError
        with a message saying what's wrong."""
        source = self.subtrees.get(tree_id)
        if source is None and self.subtree_resolver is not None:
            try:
                source = self.subtree_resolver(tree_id)
            except Exception as e:  # a resolver bug must not look like "not found"
                raise TreeBuildError(f"couldn't load behaviour tree {tree_id!r}: {e}")
            if source:
                self.subtrees[tree_id] = source
        if source is None or (isinstance(source, str) and not source.strip()):
            raise TreeBuildError(
                f"SubTree refers to behaviour tree {tree_id!r}, which doesn't exist on this robot "
                f"(check the name, and that the robot has synced since that tree was created)"
            )
        if isinstance(source, str):
            try:
                element, extra = _select_main(parse_fragment(source))
            except TreeParseError as e:
                raise TreeBuildError(f"behaviour tree {tree_id!r} can't be read: {e}")
            for other_id, other in extra.items():
                self.subtrees.setdefault(other_id, other)
            return element
        return source


def _select_main(root):
    """Accepts a bare fragment, one <BehaviorTree>, or a whole BT.CPP /
    Groot2 document. Returns (main element, {other tree ID: element})."""
    if root.tag == "BehaviorTree":
        return single_root_child(_as_root(root)), {}
    trees = [child for child in root if child.tag == "BehaviorTree"] if root.tag == "root" else []
    if not trees:
        if root.tag == "root":
            # Fragments saved by the editor sometimes carry a model block
            # next to the tree; it describes nodes, it isn't one.
            nodes = [c for c in root if c.tag != "TreeNodesModel"]
            if len(nodes) != len(list(root)):
                root = _as_root_list(nodes)
        return single_root_child(root), {}
    by_id = {t.attrib.get("ID", f"tree_{i}"): t for i, t in enumerate(trees)}
    main_id = root.attrib.get("main_tree_to_execute") or next(iter(by_id))
    if main_id not in by_id:
        raise TreeParseError(f"main_tree_to_execute={main_id!r} doesn't match any <BehaviorTree ID>")
    main = single_root_child(_as_root(by_id[main_id]))
    others = {}
    for tree_id, tree in by_id.items():
        if tree_id != main_id and len(list(tree)) == 1:
            others[tree_id] = list(tree)[0]
    return main, others


def _as_root(element):
    return _as_root_list(list(element))


def _as_root_list(children):
    import xml.etree.ElementTree as ET
    wrapper = ET.Element("root")
    wrapper.extend(children)
    return wrapper


def describe(element):
    name = element.attrib.get("name")
    return f"{element.tag}({name})" if name and name != element.tag else element.tag


def build_node(element, blackboard, ros_node=None, context=None, path=""):
    """Recursively builds one ET.Element (and its children) into a
    py_trees.behaviour.Behaviour tree. `ros_node` is the live rclpy Node
    hosting this tree (None when ticking offline, as every test in this
    repo does) -- threaded through to every builder call so leaf nodes
    with real ROS interfaces to call (Phase 10's ParamSet is the first)
    can create clients/publishers on it. Composite/control-flow builders
    ignore it; Phase 9 didn't need this parameter at all until Phase 10
    introduced the first leaf that does.
    """
    context = context or BuildContext(ros_node=ros_node)
    tag = element.tag
    path = f"{path} > {describe(element)}" if path else describe(element)

    if tag == "SubTree":
        node = _build_subtree(element, blackboard, context, path)
        return _wrap_conditional(node, element.attrib, blackboard)

    builder = NODE_REGISTRY.get(tag)
    if builder is None:
        from .builtins import UNSUPPORTED_REASONS
        reason = UNSUPPORTED_REASONS.get(tag)
        message = (f"<{tag}> isn't supported on XPARO robots: {reason}" if reason
                   else f"no registered node for tag <{tag}>")
        raise UnknownNodeError(message, path, tag)
    arity_problem = _arity_problem(tag, len(element))
    if arity_problem:
        raise TreeBuildError(arity_problem, path, tag)

    # Matches the BT editor canvas's own node-identity convention exactly
    # (frontend/.../DownloadButton.js's buildNodesFromXml: the XML `name`
    # attribute becomes the React Flow node's id, falling back to a
    # random id only when `name` is absent). executor.py's live updates
    # put this same value in node_name, and moveRobotToNode matches it
    # directly against a canvas node's id -- built from the bare tag
    # instead, every node sharing a tag (three <ParamSet> nodes in the
    # real quick_delivery_tree.xml alone) would report the identical
    # node_name, making live highlighting ambiguous or simply wrong
    # whenever a tree has more than one node of the same type. Falling
    # back to the tag when `name` is absent matches the canvas's own
    # degraded behavior for the same case.
    node_name = element.attrib.get("name") or tag
    children = [build_node(child_el, blackboard, ros_node, context, path) for child_el in element]
    try:
        node = builder(node_name, element.attrib, blackboard, children, context.ros_node if context.ros_node is not None else ros_node)
    except TreeBuildError:
        raise
    except Exception as e:
        raise TreeBuildError(f"<{tag}> couldn't be created: {e}", path, tag) from e
    # The registration/type name (the tag itself, e.g. "PlayAudio"),
    # kept distinct from node.name (the instance name above, which may be
    # a custom name="..." attribute) -- mirrors BT.CPP's own
    # node.name() vs node.registrationName() split exactly (confirmed
    # against this project's own prior C++ RosTopicLogger, which reports
    # both separately: {"node_name": node.name(), "node_type":
    # node.registrationName(), ...}). executor.py's live updates read
    # this back for the node_type field.
    node.xparo_tag = tag
    node.xparo_path = path
    return _wrap_conditional(node, element.attrib, blackboard)


def _arity_problem(tag, count):
    limits = NODE_ARITY.get(tag)
    if limits is None:
        return ""
    low, high = limits
    if high == 0 and count:
        return f"<{tag}> is a leaf node and can't have children (it has {count})"
    if high == low and count != low:
        return f"<{tag}> needs exactly {low} child{'ren' if low != 1 else ''}, it has {count}"
    if count < low:
        return f"<{tag}> needs at least {low} children, it has {count}"
    if high is not None and count > high:
        return f"<{tag}> takes at most {high} children, it has {count}"
    return ""


def _build_subtree(element, blackboard, context, path):
    tree_id = (element.attrib.get("ID") or "").strip()
    if not tree_id:
        raise TreeBuildError("SubTree has no ID -- set it to the behaviour tree to run", path, "SubTree")
    if len(element):
        raise TreeBuildError("SubTree can't have children; it runs another tree", path, "SubTree")
    if tree_id in context.stack:
        chain = " -> ".join(context.stack + [tree_id])
        raise TreeBuildError(f"SubTree loop: {chain} (a tree can't include itself)", path, "SubTree")
    if len(context.stack) >= MAX_SUBTREE_DEPTH:
        raise TreeBuildError(f"SubTrees nested more than {MAX_SUBTREE_DEPTH} deep", path, "SubTree")
    try:
        sub_root = context.find_subtree(tree_id)
    except TreeBuildError as e:
        raise TreeBuildError(e.reason, path, "SubTree") from e
    context.stack.append(tree_id)
    try:
        child = build_node(sub_root, blackboard, context.ros_node, context, path)
    finally:
        context.stack.pop()
    node = SubTreeNode(element.attrib.get("name") or tree_id, child, element.attrib, blackboard, tree_id)
    node.xparo_tag = "SubTree"
    node.xparo_path = path
    return node


def build_tree(xml_fragment, blackboard, ros_node=None, subtrees=None, subtree_resolver=None):
    """Parses a BT XML fragment and builds a py_trees tree from it.
    `blackboard` is a plain dict, shared by reference with every node in
    the tree (see executor.py's module docstring for why this engine uses
    a plain dict instead of py_trees' own global Blackboard/Client system).
    Returns the root py_trees.behaviour.Behaviour.
    """
    root_element, extra = _select_main(parse_fragment(xml_fragment))
    context = BuildContext(ros_node=ros_node, subtrees={**extra, **(subtrees or {})}, subtree_resolver=subtree_resolver)
    return build_node(root_element, blackboard, ros_node, context)


def validate_tree(xml_fragment, subtrees=None, subtree_resolver=None):
    """Checks a tree without building it. Returns a list of problems:
    {"level": "error"|"warning", "message", "path", "tag", "node"}.
    Errors mean the tree can't run; warnings are worth knowing but don't
    stop it."""
    problems = []
    try:
        root_element, extra = _select_main(parse_fragment(xml_fragment))
    except TreeParseError as e:
        return [{"level": "error", "message": str(e), "path": "", "tag": "", "node": ""}]
    context = BuildContext(subtrees={**extra, **(subtrees or {})}, subtree_resolver=subtree_resolver)

    def add(level, message, path, element):
        problems.append({"level": level, "message": message, "path": path, "tag": element.tag,
                         "node": element.attrib.get("name") or element.tag})

    node_count = [0]
    size_limit_hit = [False]

    def visit(element, path, depth=0):
        path = f"{path} > {describe(element)}" if path else describe(element)
        node_count[0] += 1
        if node_count[0] > MAX_TREE_NODES:
            if not size_limit_hit[0]:
                size_limit_hit[0] = True
                add("error", f"tree has more than {MAX_TREE_NODES} nodes -- split it into smaller SubTrees", path, element)
            return
        if depth > MAX_TREE_DEPTH:
            if not size_limit_hit[0]:
                size_limit_hit[0] = True
                add("error", f"tree is nested more than {MAX_TREE_DEPTH} levels deep -- flatten it or use a SubTree", path, element)
            return
        tag = element.tag
        for attr in _CONDITIONAL_ATTRS:
            source = element.attrib.get(attr)
            if source and not _parses(source):
                add("warning", f"{attr}={source!r} isn't a valid expression; it will count as false", path, element)
        if tag == "SubTree":
            tree_id = (element.attrib.get("ID") or "").strip()
            if not tree_id:
                add("error", "SubTree has no ID -- set it to the behaviour tree to run", path, element)
                return
            if tree_id in context.stack:
                add("error", f"SubTree loop: {' -> '.join(context.stack + [tree_id])} (a tree can't include itself)", path, element)
                return
            if len(context.stack) >= MAX_SUBTREE_DEPTH:
                add("error", f"SubTrees nested more than {MAX_SUBTREE_DEPTH} deep", path, element)
                return
            try:
                sub_root = context.find_subtree(tree_id)
            except TreeBuildError as e:
                add("error", e.reason, path, element)
                return
            context.stack.append(tree_id)
            try:
                visit(sub_root, path, depth + 1)
            finally:
                context.stack.pop()
            return
        if tag not in NODE_REGISTRY:
            from .builtins import UNSUPPORTED_REASONS
            reason = UNSUPPORTED_REASONS.get(tag)
            add("error", f"<{tag}> isn't supported on XPARO robots: {reason}" if reason else
                f"<{tag}> isn't a node this robot knows. If it's one of your custom nodes, check it "
                f"synced to the robot without errors; otherwise check the spelling", path, element)
        else:
            problem = _arity_problem(tag, len(element))
            if problem:
                add("error", problem, path, element)
            elif NODE_ARITY.get(tag, (0, 0))[1] is None and len(element) == 0:
                add("warning", f"<{tag}> has no children, so it does nothing", path, element)
            if tag == "Script" and element.attrib.get("code", "").strip():
                bad = _bad_script(element.attrib["code"])
                if bad:
                    # A warning, not an error: inside a Fallback a failing
                    # Script can be part of the plan, so the tree may run.
                    add("warning", f"Script code will fail: {bad}", path, element)
        for child in element:
            visit(child, path, depth + 1)

    visit(root_element, "")
    return problems


def _parses(expression):
    import ast
    try:
        ast.parse(expr._to_python_syntax(expression), mode="eval")
        return True
    except SyntaxError:
        return False


def _bad_script(code):
    for statement in code.split(";"):
        statement = statement.strip()
        if not statement:
            continue
        try:
            _, _, source = expr.parse_statement(statement)
        except expr.ExpressionError as e:
            return str(e)
        if not _parses(source):
            return f"{source!r} isn't a valid expression"
    return ""
