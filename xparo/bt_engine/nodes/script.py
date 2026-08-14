"""Behaviour Tree redesign Phase 9: the Script leaf node -- BT.CPP's own
"assign a blackboard variable" primitive (e.g. quick_delivery_tree.xml's
`<Script code="audio_short_punchy_path := robot_task_backend_path + '/sound/...'" />`).
Runs its code and returns SUCCESS every tick; the actual restricted
evaluation lives in expr.py, not here.
"""
import py_trees
from py_trees import common

from .. import expr


class ScriptNode(py_trees.behaviour.Behaviour):
    def __init__(self, name, code, blackboard):
        super().__init__(name=name)
        self.code = code
        self.blackboard = blackboard

    def update(self):
        if not self.code.strip():
            return common.Status.SUCCESS
        try:
            expr.run_script(self.code, self.blackboard)
            return common.Status.SUCCESS
        except expr.ExpressionError as e:
            self.feedback_message = str(e)
            return common.Status.FAILURE
