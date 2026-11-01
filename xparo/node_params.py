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

# Identity and release stage come from the launch; a live change would be
# silently ignored (the connection is already made), so the parameter
# server refuses the write instead.
READ_ONLY = ParameterDescriptor(read_only=True, description="Set at launch; relaunch xparo to change it.")


def declare_identity_parameter(node, name, default):
    """Like node.declare_parameter(name, default).value, read-only."""
    return node.declare_parameter(name, default, READ_ONLY).value


def declare_secret_parameter(node, name, default):
    """Returns the value the launch supplied, then replaces the parameter
    with a read-only HIDDEN placeholder so the real value is gone from the
    parameter server (ignore_override: otherwise the launch override would
    come straight back on re-declaration). The dashboard's ROS2 Parameters
    tool already masked it; this closes the `ros2 param get` path."""
    value = node.declare_parameter(name, default).value
    node.undeclare_parameter(name)
    node.declare_parameter(name, HIDDEN, READ_ONLY, ignore_override=True)
    return value
