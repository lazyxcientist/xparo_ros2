"""STUB -- this repo already subscribes to Nav2's /behavior_tree_log
(xparo_ros.py's navigation_callback) but has no publisher or action client
for anything Nav2-related, so there's no real NavigateToPose action client
to send a goal through yet. Commented out in the one real example tree
today (quick_delivery_tree.xml) but kept implemented since it's clearly
intended to be used once uncommented.
# TODO(hardware): wire to Nav2's real NavigateToPose action client,
# resolving `location` against wherever this project's map-location data
# actually lives (not yet defined at planning time).
See nodes/base.py's StubActionNode docstring for the honest-scoping
rationale shared by every stub in this package.
"""
from .base import StubActionNode


class NavigateToNode(StubActionNode):
    TAG = "NavigateTo"
    REQUIRED_ATTRS = ("location",)
