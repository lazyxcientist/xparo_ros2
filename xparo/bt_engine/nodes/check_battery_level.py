"""STUB -- no battery-telemetry subsystem exists in this repo yet (the
real value would need to come from a real /battery_state-style topic this
repo doesn't subscribe to anywhere today). Always resolves SUCCESS rather
than actually comparing against min_level -- this is a condition node
pretending to always pass, not a real check, called out explicitly here
rather than silently faked as meaningful.
# TODO(hardware): subscribe to real battery telemetry and compare its
# current level against the resolved min_level attribute for real.
See nodes/base.py's StubActionNode docstring for the honest-scoping
rationale shared by every stub in this package.
"""
from .base import StubActionNode


class CheckBatteryLevelNode(StubActionNode):
    TAG = "CheckBatteryLevel"
    REQUIRED_ATTRS = ("min_level",)
