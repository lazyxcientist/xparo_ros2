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


def test_the_secret_never_appears_on_parameter_events(monkeypatch):
    """R3 review: declaring the parameter with the launch's value first
    published it in a ParameterEvent (new_parameters) before the HIDDEN
    swap, so `ros2 topic echo /parameter_events` on the site LAN printed the
    key at every start. A real listener, on a domain of its own."""
    from rcl_interfaces.msg import ParameterEvent
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_parameter_events
    import time

    monkeypatch.setenv("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
    context = rclpy.Context()
    rclpy.init(context=context, domain_id=177)
    executor = SingleThreadedExecutor(context=context)
    seen = []
    try:
        listener = rclpy.create_node("parameter_events_listener", context=context)
        listener.create_subscription(ParameterEvent, "/parameter_events", seen.append, qos_profile_parameter_events)
        node = rclpy.create_node("node_params_secret_probe", context=context, parameter_overrides=[
            Parameter("xparo_secret_key", value="sk-REAL-SECRET")])
        executor.add_node(listener)
        deadline = time.monotonic() + 10
        while node._parameter_event_publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)

        assert node_params.declare_secret_parameter(node, "xparo_secret_key", "") == "sk-REAL-SECRET"

        def from_probe():
            return [(p.name, p.value.string_value) for event in seen if event.node == "/node_params_secret_probe"
                    for p in [*event.new_parameters, *event.changed_parameters, *event.deleted_parameters]]
        while ("xparo_secret_key", node_params.HIDDEN) not in from_probe() and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)

        assert ("xparo_secret_key", node_params.HIDDEN) in from_probe()  # the listener did hear the node
        assert all("sk-REAL-SECRET" not in value for _name, value in from_probe())
    finally:
        executor.shutdown()
        rclpy.shutdown(context=context)


def test_identity_parameters_refuse_a_live_set(node):
    assert node_params.declare_identity_parameter(node, "xparo_project_id", "") == "proj-1"
    node_params.declare_secret_parameter(node, "xparo_secret_key", "")

    for name in ("xparo_project_id", "xparo_secret_key"):
        result = node.set_parameters([Parameter(name, value="something-else")])[0]
        assert result.successful is False, name
        assert "read-only" in result.reason

    assert node.get_parameter("xparo_project_id").value == "proj-1"
    assert node.get_parameter("xparo_secret_key").value == node_params.HIDDEN


def test_each_read_only_parameter_describes_itself(node):
    """ROSAPI-12: one shared descriptor object -> `ros2 param describe /xparo
    xparo_secret_key` printed "Parameter name: xparo_folder"."""
    node_params.declare_identity_parameter(node, "xparo_project_id", "")
    node_params.declare_secret_parameter(node, "xparo_secret_key", "")
    node_params.declare_launch_parameter(node, "xparo_folder", "/opt/xparo")

    names = [d.name for d in node.describe_parameters(["xparo_project_id", "xparo_secret_key", "xparo_folder"])]

    assert names == ["xparo_project_id", "xparo_secret_key", "xparo_folder"]


def test_launch_only_parameters_refuse_a_live_set(node):
    """ROSAPI-3: transport, paths, BAG_DIR and record_bags said "Set
    parameter successful" and nothing used the new value."""
    node_params.declare_launch_parameter(node, "xparo_transport", "django_ws")
    node_params.declare_launch_parameter(node, "BAG_DIR", "/tmp/bags")
    node_params.declare_launch_parameter(node, "record_bags", False)

    for name, value in (("xparo_transport", "tethered_tcp"), ("BAG_DIR", "/elsewhere"), ("record_bags", True)):
        result = node.set_parameters([Parameter(name, value=value)])[0]
        assert result.successful is False, name
        assert "read-only" in result.reason

    assert node.describe_parameters(["record_bags"])[0].description == node_params.READ_ONLY_DESCRIPTION


def test_the_node_declares_its_launch_only_parameters_read_only():
    import pathlib
    import re
    source = (pathlib.Path(node_params.__file__).parent / "xparo_ros.py").read_text()
    writable = re.findall(r'self\.declare_parameter\(\s*"(\w+)"', source)
    assert writable == []
