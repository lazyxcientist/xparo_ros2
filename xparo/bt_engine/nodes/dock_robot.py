"""STUB -- no docking subsystem exists in this repo yet.
# TODO(hardware): wire to the real docking subsystem (Nav2's docking
# server, or whatever action/service this robot's actual dock hardware
# exposes) -- dock_method distinguishes "docking" from "undocking".
See nodes/base.py's StubActionNode docstring for the honest-scoping
rationale shared by every stub in this package.
"""
from .base import StubActionNode


class DockRobotNode(StubActionNode):
    TAG = "DockRobot"
    REQUIRED_ATTRS = ("dock_method",)
