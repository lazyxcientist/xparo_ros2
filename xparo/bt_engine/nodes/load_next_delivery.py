"""STUB -- no delivery-queue subsystem exists in this repo yet (no real
"what's the next delivery" data source to load from). Takes no attributes
in the one real example tree that uses it.
# TODO(hardware): wire to a real delivery-queue source (Django-resolved
# task params are the most likely real answer here, once Phase 11 defines
# what data actually flows into a running task).
See nodes/base.py's StubActionNode docstring for the honest-scoping
rationale shared by every stub in this package.
"""
from .base import StubActionNode


class LoadNextDeliveryNode(StubActionNode):
    TAG = "LoadNextDelivery"
    REQUIRED_ATTRS = ()
