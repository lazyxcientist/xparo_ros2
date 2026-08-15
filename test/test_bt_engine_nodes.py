"""Behaviour Tree redesign Phase 10 (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): concrete leaf node implementations.
FakeClient/FakeFuture below mirror test_rosbag_control.py's own fake-ROS
harness (a minimal add_done_callback-free stand-in), not imported from
there since each test file in this repo is self-contained by convention.
"""
import time
from unittest.mock import MagicMock

import pytest
from py_trees import common

from xparo.bt_engine.nodes.check_battery_level import CheckBatteryLevelNode
from xparo.bt_engine.nodes.dock_robot import DockRobotNode
from xparo.bt_engine.nodes.load_next_delivery import LoadNextDeliveryNode
from xparo.bt_engine.nodes.notify_patient import NotifyPatientNode
from xparo.bt_engine.nodes.param_set import ParamSetNode, build_parameter_value
from xparo.bt_engine.nodes.play_audio import PlayAudioNode
from xparo.bt_engine.nodes.speak_text import SpeakTextNode
from xparo.bt_engine.nodes.navigate_to import NavigateToNode


def _tick_to_completion(node, max_ticks=20):
    for _ in range(max_ticks):
        status = node.update()
        if status != common.Status.RUNNING:
            return status
        time.sleep(0.01)
    return common.Status.RUNNING


class TestStubNodes:
    @pytest.mark.parametrize(
        "cls,attrs",
        [
            (PlayAudioNode, {"file_path": "{sound_path}"}),
            (DockRobotNode, {"dock_method": "docking"}),
            (CheckBatteryLevelNode, {"min_level": "20.0"}),
            (SpeakTextNode, {"text": "{message}"}),
            (NotifyPatientNode, {"tray": "A"}),
            (LoadNextDeliveryNode, {}),
            (NavigateToNode, {"location": "{dest}"}),
        ],
    )
    def test_stub_resolves_and_succeeds(self, cls, attrs):
        blackboard = {"sound_path": "/opt/sound.mp3", "message": "hello", "dest": "bed-3"}
        node = cls(name=cls.__name__, attrs=attrs, blackboard=blackboard)
        node.SIMULATED_DELAY_S = 0  # don't slow the suite down over a stub's fake delay
        status = _tick_to_completion(node)
        assert status == common.Status.SUCCESS

    @pytest.mark.parametrize(
        "cls,attrs",
        [
            (PlayAudioNode, {"file_path": "{never_set}"}),
            (DockRobotNode, {}),
            (SpeakTextNode, {"text": "{never_set}"}),
        ],
    )
    def test_stub_fails_when_a_required_attr_is_unresolved(self, cls, attrs):
        node = cls(name=cls.__name__, attrs=attrs, blackboard={})
        assert node.update() == common.Status.FAILURE

    def test_stub_holds_running_for_the_simulated_delay(self):
        node = PlayAudioNode(name="p", attrs={"file_path": "/x.mp3"}, blackboard={})
        node.SIMULATED_DELAY_S = 10  # long enough that the first tick can't have finished
        assert node.update() == common.Status.RUNNING


class FakeFuture:
    def __init__(self, result=None, exception=None, done=True):
        self._result = result
        self._exception = exception
        self._done = done

    def done(self):
        return self._done

    def result(self):
        if self._exception:
            raise self._exception
        return self._result


class FakeSetParametersResult:
    def __init__(self, successful, reason=""):
        self.successful = successful
        self.reason = reason


class FakeClient:
    def __init__(self, ready=True, future=None):
        self.ready = ready
        self.future = future or FakeFuture(result=MagicMock(results=[FakeSetParametersResult(True)]))
        self.calls = []

    def service_is_ready(self):
        return self.ready

    def call_async(self, request):
        self.calls.append(request)
        return self.future


class FakeRosNode:
    def __init__(self, client):
        self._client = client
        self.created_with = []

    def create_client(self, srv_type, name):
        self.created_with.append(name)
        return self._client


class TestParamSet:
    def test_build_parameter_value_scalar_types(self):
        assert build_parameter_value("double", "1.5").double_value == 1.5
        assert build_parameter_value("int", "3").integer_value == 3
        assert build_parameter_value("string", "hi").string_value == "hi"
        assert build_parameter_value("bool", "true").bool_value is True
        assert build_parameter_value("bool", "false").bool_value is False

    def test_build_parameter_value_array_from_json_string(self):
        pv = build_parameter_value("double_array", "[1, 2, 3]")
        assert list(pv.double_array_value) == [1.0, 2.0, 3.0]

    def test_build_parameter_value_array_from_comma_string(self):
        pv = build_parameter_value("double_array", "1,2,3")
        assert list(pv.double_array_value) == [1.0, 2.0, 3.0]

    def test_build_parameter_value_array_from_real_list(self):
        pv = build_parameter_value("integer_array", [1, 2, 3])
        assert list(pv.integer_array_value) == [1, 2, 3]

    def test_build_parameter_value_unsupported_type_raises(self):
        with pytest.raises(ValueError):
            build_parameter_value("not_a_real_type", "1")

    def test_succeeds_when_the_service_accepts_the_call(self):
        client = FakeClient(ready=True)
        ros_node = FakeRosNode(client)
        node = ParamSetNode(
            name="p",
            attrs={"node_name": "velocity_smoother", "param_name": "max_velocity",
                   "param_value": "[1,1,1]", "param_type": "double_array"},
            blackboard={},
            ros_node=ros_node,
        )
        assert node.update() == common.Status.RUNNING  # call issued, future not yet consumed
        assert node.update() == common.Status.SUCCESS  # future is already-done in this fake
        assert ros_node.created_with == ["/velocity_smoother/set_parameters"]

    def test_fails_when_the_service_rejects_the_parameter(self):
        client = FakeClient(future=FakeFuture(result=MagicMock(
            results=[FakeSetParametersResult(False, reason="read-only parameter")]
        )))
        node = ParamSetNode(
            name="p",
            attrs={"node_name": "global_costmap", "param_name": "inflation_radius",
                   "param_value": "0.5", "param_type": "double"},
            blackboard={},
            ros_node=FakeRosNode(client),
        )
        node.update()
        assert node.update() == common.Status.FAILURE

    def test_waits_while_the_service_is_not_ready(self):
        client = FakeClient(ready=False)
        node = ParamSetNode(
            name="p",
            attrs={"node_name": "n", "param_name": "p", "param_value": "1", "param_type": "double"},
            blackboard={},
            ros_node=FakeRosNode(client),
        )
        assert node.update() == common.Status.RUNNING
        assert client.calls == []  # never actually called while not ready

    def test_no_ros_node_attached_succeeds_without_calling_anything(self):
        node = ParamSetNode(
            name="p",
            attrs={"node_name": "n", "param_name": "p", "param_value": "1", "param_type": "double"},
            blackboard={},
            ros_node=None,
        )
        assert node.update() == common.Status.SUCCESS

    def test_fails_when_a_required_attribute_is_unresolved(self):
        node = ParamSetNode(
            name="p",
            attrs={"node_name": "{never_set}", "param_name": "p", "param_value": "1", "param_type": "double"},
            blackboard={},
            ros_node=FakeRosNode(FakeClient()),
        )
        assert node.update() == common.Status.FAILURE
