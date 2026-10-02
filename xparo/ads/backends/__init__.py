"""Display backends for AdManager.

    native   xparo's own player (Tk + ffplay) -- public robots
    xpshell  hand the schedule to XP-shell's Kivy player over ROS2 -- robots running XP-shell
    off      no ads on this robot
"""
import logging

from .base import DisplayBackend, OffBackend

log = logging.getLogger("xparo.ads")
CHOICES = ("native", "xpshell", "off")


def make_backend(name, node=None):
    """The backend for the xparo_ads_display launch argument. Falls back to
    "off" (with a warning) when the chosen one can't run here."""
    name = (name or "off").strip().lower()
    if name == "native":
        from .native import NativeBackend
        return NativeBackend()
    if name == "xpshell":
        if node is None:
            log.warning("xparo_ads_display:=xpshell needs the ROS 2 node; ads are off")
            return OffBackend()
        from .ros2 import Ros2Backend
        return Ros2Backend(node)
    if name != "off":
        log.warning("unknown xparo_ads_display %r; ads are off", name)
    return OffBackend()
