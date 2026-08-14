"""Behaviour Tree redesign Phase 9: a deliberately restricted expression
evaluator for Script nodes ("name := expr", used to assign blackboard
variables mid-tree, e.g. quick_delivery_tree.xml's
`audio_short_punchy_path := robot_task_backend_path + '/sound/...'`) and
the BT.CPP-style bare conditional attributes (_skipIf/_successIf/
_failureIf/_while) tree_builder.py wraps every node with.

Deliberately NOT Python's eval() -- ros_packages/src/xparo already carries
a precedent against exactly that shortcut: engine.py's predecessor had an
unauthenticated eval() RCE that was removed, with test_engine.py's
test_eval_key_is_not_specially_handled guarding against it coming back.
This walks a whitelisted subset of Python's own ast instead of executing
arbitrary code, after translating BT.CPP's C-like operators (!, &&, ||)
into the Python syntax ast.parse understands -- the real XML in this repo
uses BT.CPP syntax (`_skipIf="!docking_feature_enabled"`), not Python's.
"""
import ast
import operator
import re

_BANG_NOT_EQ_RE = re.compile(r"!(?!=)")


class ExpressionError(Exception):
    pass


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _to_python_syntax(expression):
    """Translates BT.CPP's C-like scripting operators to Python's, without
    touching '!=' (already valid Python -- the negative lookahead keeps a
    bare '!' from being confused with it)."""
    expression = _BANG_NOT_EQ_RE.sub("not ", expression)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    return expression


def evaluate(expression, blackboard):
    """Evaluates a restricted expression against blackboard (a plain
    dict). Supports identifiers (blackboard lookups), string/number/bool
    literals, +/-/*//, unary -/not, comparisons, and/or -- enough for
    every real Script/_skipIf usage in this repo's example trees, and
    deliberately nothing more (no calls, no attribute/subscript access, no
    imports)."""
    try:
        node = ast.parse(_to_python_syntax(expression), mode="eval").body
    except SyntaxError as e:
        raise ExpressionError(f"invalid expression {expression!r}: {e}") from e
    return _eval_node(node, blackboard)


def _eval_node(node, blackboard):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in blackboard:
            raise ExpressionError(f"undefined variable {node.id!r}")
        return blackboard[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](
            _eval_node(node.left, blackboard), _eval_node(node.right, blackboard)
        )
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, blackboard)
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand, blackboard)
        raise ExpressionError(f"unsupported unary operator: {ast.dump(node.op)}")
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP_OPS:
        return _CMP_OPS[type(node.ops[0])](
            _eval_node(node.left, blackboard), _eval_node(node.comparators[0], blackboard)
        )
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, blackboard) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    raise ExpressionError(f"unsupported expression element: {ast.dump(node)}")


def run_script(code, blackboard):
    """Executes a Script node's `code` attribute -- one or more
    'name := expr' assignments, ';'-separated. Mutates blackboard in
    place; returns the set of names it assigned (bt_params.py on the
    Django side uses the same "Script-assigned names are internal, not a
    param the dashboard needs to ask for" rule this mirrors)."""
    assigned = set()
    for statement in code.split(";"):
        statement = statement.strip()
        if not statement:
            continue
        if ":=" not in statement:
            raise ExpressionError(f"expected 'name := expr' in Script code, got {statement!r}")
        name, _, expr_src = statement.partition(":=")
        name = name.strip()
        if not name.isidentifier():
            raise ExpressionError(f"invalid assignment target {name!r}")
        blackboard[name] = evaluate(expr_src.strip(), blackboard)
        assigned.add(name)
    return assigned


def evaluate_condition(expression, blackboard):
    """For _skipIf/_successIf/_failureIf/_while. A referenced variable
    that isn't in the blackboard is treated as falsy rather than an error
    -- bt_params.py's extract_blackboard_params deliberately excludes
    these bare (non-"{}") references from the params the Task Behaviour
    tab asks the user to map, so most of them will genuinely never be in
    the resolved blackboard unless a Script node in the same tree sets
    them. Crashing a whole tree tick over an unset optional feature flag
    would be worse than treating it as off."""
    try:
        return bool(evaluate(expression, blackboard))
    except ExpressionError:
        return False
