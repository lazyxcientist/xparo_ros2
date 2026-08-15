"""STUB -- no patient-notification subsystem exists in this repo yet.
# TODO(hardware): wire to whatever this robot actually uses to notify a
# patient/recipient a delivery has arrived (screen, light, buzzer, app
# push -- unspecified at planning time).
See nodes/base.py's StubActionNode docstring for the honest-scoping
rationale shared by every stub in this package.
"""
from .base import StubActionNode


class NotifyPatientNode(StubActionNode):
    TAG = "NotifyPatient"
    REQUIRED_ATTRS = ("tray",)
