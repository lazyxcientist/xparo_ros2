"""Example custom BT Condition node (Python) -- see greet_example.py's
module docstring for why this ships in the repo. A real deployment would
read this robot's actual battery telemetry (there's no such subsystem in
this repo yet, matching bt_engine/nodes/*.py's own honest-scoping stubs)
-- this uses a fixed simulated reading so the example is deterministic
and testable without any hardware at all.
"""
from xparo.bt_engine.plugin_loader import CustomBTNode
from xparo.bt_engine.nodes.base import resolve_attrs
from py_trees.common import Status

# TODO(hardware): replace with a real battery-telemetry read.
_SIMULATED_BATTERY_PERCENT = 76.0


class BatteryOkExample(CustomBTNode):
    XML_TAG = "BatteryOkExample"

    def update(self):
        attrs = resolve_attrs(self.attrs, self.blackboard, required=())
        try:
            min_level = float(attrs.get("min_level") or 20.0)
        except (TypeError, ValueError):
            return Status.FAILURE

        ok = _SIMULATED_BATTERY_PERCENT >= min_level
        print(f"[BatteryOkExample] battery={_SIMULATED_BATTERY_PERCENT}% min_level={min_level} -> {'SUCCESS' if ok else 'FAILURE'}")
        return Status.SUCCESS if ok else Status.FAILURE
