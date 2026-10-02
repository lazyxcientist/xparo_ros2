"""Ads on the robot's screen (XPARO Ads Center).

The server sends this robot the ads its owner approved (ads_schedule);
AdManager keeps them on disk, downloads their files, and has a display
backend play them on schedule -- xparo's own player on public robots
(backends/native.py) or XP-shell's Kivy player through ROS2
(backends/ros2.py). Every play is written to a local log and uploaded
(ADS_PLAYS), so owners and customers see exactly what played, even after
the robot was offline for a while.
"""
from .manager import AdManager  # noqa: F401
