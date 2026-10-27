"""Shared test setup.

Makes the package importable from source (no colcon build needed), and,
only when ROS 2 itself is NOT installed (a laptop or CI box without ROS),
puts small stand-ins for the ROS Python packages in sys.modules so the
pure-Python logic (engine.py, transports, bt_engine, ...) can still be
tested. With ROS 2 sourced, the real packages are used and none of this
applies.
"""
import os
import sys
import types
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_ros_stand_ins():
    class _Node:
        """Enough of rclpy.node.Node for xparo_ros.Xparo to subclass."""

        def __init__(self, *args, **kwargs):
            self._logger = MagicMock()

        def get_logger(self):
            return self._logger

        def __getattr__(self, name):  # create_subscription, create_publisher, declare_parameter...
            return MagicMock(name=name)

    class ExternalShutdownException(Exception):
        pass

    modules = {}
    for name in ("rclpy", "rclpy.node", "rclpy.qos", "rclpy.executors", "rclpy.parameter",
                 "std_msgs", "std_msgs.msg", "sensor_msgs", "sensor_msgs.msg",
                 "diagnostic_msgs", "diagnostic_msgs.msg", "rcl_interfaces", "rcl_interfaces.msg",
                 "rcl_interfaces.srv", "rosbag2_interfaces", "rosbag2_interfaces.srv",
                 "nav2_msgs", "nav2_msgs.msg", "builtin_interfaces", "builtin_interfaces.msg",
                 "ament_index_python", "ament_index_python.packages"):
        module = MagicMock(name=name)
        module.__name__ = name
        module.__path__ = []
        modules[name] = module
    modules["rclpy.node"].Node = _Node
    modules["rclpy.executors"].ExternalShutdownException = ExternalShutdownException

    class String:
        def __init__(self, data=""):
            self.data = data

    modules["std_msgs.msg"].String = String
    sys.modules.update(modules)
    sys.modules.setdefault("xparo_test_ros_stand_ins", types.ModuleType("xparo_test_ros_stand_ins"))


try:
    import rclpy  # noqa: F401
except ImportError:
    _install_ros_stand_ins()

try:
    from launch import LaunchDescription  # noqa: F401  (this repo's own launch/ folder isn't it)
except ImportError:
    for _name in ("launch", "launch.actions", "launch.conditions", "launch.substitutions",
                  "launch_ros", "launch_ros.actions"):
        sys.modules.pop(_name, None)
    for _name in ("launch", "launch.actions", "launch.conditions", "launch.substitutions",
                  "launch_ros", "launch_ros.actions"):
        sys.modules.setdefault(_name, MagicMock(name=_name))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path_factory, monkeypatch):
    """Engine keeps unsent task results (outbox.json) and the robot identity
    in its state dir; each test gets its own, so nothing leaks between
    tests or into the source tree."""
    state = tmp_path_factory.mktemp("xparo_state")
    monkeypatch.setenv("XPARO_STATE_DIR", str(state))
    monkeypatch.setenv("XPARO_HOME", str(state / "home"))
    monkeypatch.setenv("XPARO_OTA_JOURNAL", str(state / "ota_journal.json"))
