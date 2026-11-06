"""The xparo node's identity parameters (xparo_ros.py): declared read-only,
and the secret key is not kept as a parameter value at all.

`ros2 param get /xparo xparo_secret_key` used to print the full project
secret key to anyone on the ROS domain (the default ROS_DOMAIN_ID 0
reaches the whole site LAN -- enough to join a rogue robot to the fleet),
and `ros2 param set` on any identity parameter answered "Set parameter
successful" while the node kept its old connection, so `param get` then
reported a value the robot wasn't using (ROSAPI-2/3, 2026-10-31 field
test). Separate from xparo_ros.py so it can be tested with a bare rclpy
Node, without that module's package-share lookups.
"""
from rcl_interfaces.msg import ParameterDescriptor

HIDDEN = "(hidden)"

# Identity, release stage, transport and file paths come from the launch;
# a live change would be silently ignored (the connection is made, the
# paths are read once), so the parameter server refuses the write instead.
READ_ONLY_DESCRIPTION = "Set at launch; relaunch xparo to change it."


def _read_only(name):
    # One descriptor per parameter: declare_parameter writes the name into
    # the object it is given, so a shared one made `ros2 param describe`
    # call every read-only parameter "xparo_folder" (ROSAPI-12).
    return ParameterDescriptor(name=name, read_only=True, description=READ_ONLY_DESCRIPTION)


def declare_launch_parameter(node, name, default):
    """Like node.declare_parameter(name, default).value, read-only."""
    return node.declare_parameter(name, default, _read_only(name)).value


declare_identity_parameter = declare_launch_parameter


def declare_secret_parameter(node, name, default):
    """Returns the value the launch supplied and declares the parameter as
    a read-only HIDDEN placeholder (ignore_override: the launch's value
    never reaches the parameter server). The value is read from the node's
    overrides, not by declaring it: every declaration publishes the value
    on /parameter_events, so declaring it first and swapping in HIDDEN
    still showed the key to `ros2 topic echo /parameter_events` (R3 review).
    The dashboard's ROS2 Parameters tool already masked it."""
    # rclpy has no public reader for a node's overrides; this is the dict
    # declare_parameter itself reads them from.
    override = getattr(node, "_parameter_overrides", {}).get(name)
    value = default if override is None else override.value
    node.declare_parameter(name, HIDDEN, _read_only(name), ignore_override=True)
    return value
