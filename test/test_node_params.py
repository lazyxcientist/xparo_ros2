"""node_params.py: the xparo node's identity parameters are read-only and
the secret key is never readable on the ROS graph (ROSAPI-2/3, 2026-10-31
field test: `ros2 param get /xparo xparo_secret_key` printed the project
secret to anyone on the site LAN, and `ros2 param set` on an identity
parameter said "successful" while the node kept its old connection).

Uses a bare rclpy Node with parameter overrides standing in for the
launch's `xparo_secret_key:=...` arguments -- xparo_ros.py itself needs the
installed package's share directory to import -- on its own rclpy Context
so it neither depends on nor disturbs any other test's.
"""
import pytest

rclpy = pytest.importorskip("rclpy")
from rclpy.parameter import Parameter

from xparo import node_params


@pytest.fixture
def node():
    context = rclpy.Context()
    rclpy.init(context=context)
    node = rclpy.create_node("node_params_under_test", context=context, parameter_overrides=[
        Parameter("xparo_secret_key", value="real-secret-123"),
        Parameter("xparo_project_id", value="proj-1"),
    ])
    yield node
    node.destroy_node()
    rclpy.shutdown(context=context)


def test_the_secret_reaches_the_node_but_is_hidden_on_the_parameter_server(node):
    assert node_params.declare_secret_parameter(node, "xparo_secret_key", "") == "real-secret-123"
    # What `ros2 param get` / `ros2 param dump` now see.
    assert node.get_parameter("xparo_secret_key").value == node_params.HIDDEN


def test_identity_parameters_refuse_a_live_set(node):
    assert node_params.declare_identity_parameter(node, "xparo_project_id", "") == "proj-1"
    node_params.declare_secret_parameter(node, "xparo_secret_key", "")

    for name in ("xparo_project_id", "xparo_secret_key"):
        result = node.set_parameters([Parameter(name, value="something-else")])[0]
        assert result.successful is False, name
        assert "read-only" in result.reason

    assert node.get_parameter("xparo_project_id").value == "proj-1"
    assert node.get_parameter("xparo_secret_key").value == node_params.HIDDEN
