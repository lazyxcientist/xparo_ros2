"""Hand ads to XP-shell (the Kivy shell running on the robot's screen) over ROS 2.

    xparo/ads/schedule  (std_msgs/String, JSON, latched)   xparo -> XP-shell
        {"ads": [<schedule entry with a local "path">...], "generated_at": ...}
    xparo/ads/plays     (std_msgs/String, JSON)            XP-shell -> xparo
        one play record (XP-shell's services/ads/logger_db.py row)

The schedule is published "transient local", so an XP-shell that starts
after xparo still receives the latest one. XP-shell's side lives in its
services/xparo_cloud/ros_ads.py.
"""
import json
import logging

from .base import DisplayBackend

log = logging.getLogger("xparo.ads")

SCHEDULE_TOPIC = "xparo/ads/schedule"
PLAYS_TOPIC = "xparo/ads/plays"


def schedule_message(items):
    """What XP-shell receives: only xparo's own ads, each with its local file path."""
    return json.dumps({"ads": [dict(item) for item in items if str(item.get("id") or "").startswith("xparo-")]})


class Ros2Backend(DisplayBackend):
    name = "xpshell"
    plays_itself = False

    def __init__(self, node):
        self.node = node
        self._publisher = None

    def start(self, manager):
        super().start(manager)
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String
        self._string = String
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
        self._publisher = self.node.create_publisher(String, SCHEDULE_TOPIC, latched)
        self.node.create_subscription(String, PLAYS_TOPIC, self._on_play, 50)

    def schedule_changed(self, items):
        if self._publisher is None:
            return
        msg = self._string()
        msg.data = schedule_message(items)
        self._publisher.publish(msg)

    def _on_play(self, msg):
        try:
            record = json.loads(msg.data)
        except (TypeError, ValueError):
            log.warning("ignored a malformed play report from XP-shell")
            return
        if str(record.get("ad_id") or "").startswith("xparo-"):
            self.manager.record_external_play(record)
