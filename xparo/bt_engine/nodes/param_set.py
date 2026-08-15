"""Behaviour Tree redesign Phase 10: ParamSet -- the one leaf tag this repo
already has real infrastructure to hook into. Every ROS2 node exposes a
standard rcl_interfaces/srv/SetParameters service at
/<node_name>/set_parameters; this calls it for real (matching BT.CPP's own
ParamSet node semantics), unlike every other leaf in this package which
ships as a clearly-marked stub for lack of any real subsystem to call.
"""
import json

import py_trees
from py_trees import common
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters

from .base import BlackboardKeyError, resolve_attrs

_TYPE_MAP = {
    "bool": (ParameterType.PARAMETER_BOOL, "bool_value", bool),
    "int": (ParameterType.PARAMETER_INTEGER, "integer_value", int),
    "integer": (ParameterType.PARAMETER_INTEGER, "integer_value", int),
    "double": (ParameterType.PARAMETER_DOUBLE, "double_value", float),
    "string": (ParameterType.PARAMETER_STRING, "string_value", str),
    "bool_array": (ParameterType.PARAMETER_BOOL_ARRAY, "bool_array_value", bool),
    "integer_array": (ParameterType.PARAMETER_INTEGER_ARRAY, "integer_array_value", int),
    "double_array": (ParameterType.PARAMETER_DOUBLE_ARRAY, "double_array_value", float),
    "string_array": (ParameterType.PARAMETER_STRING_ARRAY, "string_array_value", str),
}

_TRUTHY_STRINGS = ("1", "true", "yes", "on")


def _coerce_array(value, item_caster):
    """param_value for an *_array param_type may already be a real list
    (Phase 11 could resolve it that way) or a string -- try JSON first
    (covers "[1,2,3]" cleanly), then fall back to a bare comma-split
    (covers "1,2,3")."""
    if isinstance(value, (list, tuple)):
        return [item_caster(v) for v in value]
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (list, tuple)):
            return [item_caster(v) for v in parsed]
    except (TypeError, ValueError):
        pass
    return [item_caster(v.strip()) for v in str(value).split(",") if v.strip() != ""]


def build_parameter_value(param_type, raw_value):
    if param_type not in _TYPE_MAP:
        raise ValueError(f"unsupported param_type {param_type!r}")
    ros_type, field_name, caster = _TYPE_MAP[param_type]
    pv = ParameterValue()
    pv.type = ros_type
    if param_type.endswith("_array"):
        setattr(pv, field_name, _coerce_array(raw_value, caster))
    elif param_type == "bool":
        value = raw_value if isinstance(raw_value, bool) else str(raw_value).strip().lower() in _TRUTHY_STRINGS
        setattr(pv, field_name, value)
    else:
        setattr(pv, field_name, caster(raw_value))
    return pv


class ParamSetNode(py_trees.behaviour.Behaviour):
    def __init__(self, name, attrs, blackboard, ros_node):
        super().__init__(name=name)
        self.attrs = attrs
        self.blackboard = blackboard
        self.ros_node = ros_node
        self._client = None
        self._client_target = None
        self._future = None

    def initialise(self):
        self._future = None

    def _client_for(self, target_node_name):
        if self.ros_node is None:
            return None
        if self._client is None or self._client_target != target_node_name:
            self._client = self.ros_node.create_client(SetParameters, f"/{target_node_name}/set_parameters")
            self._client_target = target_node_name
        return self._client

    def update(self):
        try:
            resolved = resolve_attrs(
                self.attrs, self.blackboard,
                required=("node_name", "param_name", "param_value", "param_type"),
            )
        except BlackboardKeyError as e:
            self.feedback_message = str(e)
            return common.Status.FAILURE

        if self._future is None:
            try:
                param_value = build_parameter_value(resolved["param_type"], resolved["param_value"])
            except (ValueError, TypeError) as e:
                self.feedback_message = f"bad param_value for param_type={resolved['param_type']!r}: {e}"
                return common.Status.FAILURE

            client = self._client_for(resolved["node_name"])
            if client is None:
                # No live ROS node attached (ticking offline, as every
                # test in this repo does, or via a dashboard "preview"
                # run) -- nothing to call. Not a failure: exercising tree
                # logic without hardware is the whole point of that case.
                self.feedback_message = f"[no ROS node attached] would set {resolved['node_name']}.{resolved['param_name']}"
                return common.Status.SUCCESS
            if not client.service_is_ready():
                self.feedback_message = f"waiting for /{resolved['node_name']}/set_parameters"
                return common.Status.RUNNING

            request = SetParameters.Request()
            request.parameters = [Parameter(name=resolved["param_name"], value=param_value)]
            self._future = client.call_async(request)
            return common.Status.RUNNING

        if not self._future.done():
            return common.Status.RUNNING

        future, self._future = self._future, None
        try:
            response = future.result()
        except Exception as e:
            self.feedback_message = f"set_parameters call failed: {e}"
            return common.Status.FAILURE

        failed = [r.reason for r in response.results if not r.successful]
        if failed:
            self.feedback_message = f"set_parameters rejected: {'; '.join(failed)}"
            return common.Status.FAILURE
        return common.Status.SUCCESS
