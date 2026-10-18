"""Example custom BT Action node (Python) -- ships with the repo so the
multi-language custom-node pipeline is testable with zero Django/dashboard
interaction: a fresh `colcon build --symlink-install` alone registers this
tag (see engine.py's sync_custom_node_files, examples_manifest.json).
Genuinely working, not a TODO stub -- writes a real greeting to its
output port every tick.
"""
from xparo.bt_engine.plugin_loader import CustomBTNode
from xparo.bt_engine.nodes.base import resolve_attrs, write_output
from py_trees.common import Status


class GreetExample(CustomBTNode):
    XML_TAG = "GreetExample"

    def update(self):
        attrs = resolve_attrs(self.attrs, self.blackboard, required=())
        name = attrs.get("name") or "robot"

        greeting = f"Hello, {name}! XPARO custom node pipeline is working."
        print(f"[GreetExample] {greeting}")

        write_output(self.attrs, self.blackboard, "greeting", greeting)
        return Status.SUCCESS

    def halt(self):
        super().halt()
