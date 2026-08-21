"""Multi-language custom BT node system, Phase 6/7: process_runner.py's
ProcessBackedNode (the shared out-of-process base for JS/C++) -- edge
cases not already covered by test_custom_node_files_sync.py's happy-path
coverage: halt/termination, a hung child, output writing, crash isolation
for a process that exits unexpectedly. Uses a real python3 -c one-liner as
the "child process" (fast, no real language runner needed to test the
generic protocol machinery itself) -- matching this repo's own established
"exercise the real subprocess, don't mock" convention (test_remote_ops.py).
"""
import sys

import pytest
from py_trees import common

from xparo.bt_engine.process_runner import ProcessBackedNode

# A minimal fake "child" speaking the real wire protocol -- one JSON line
# in, one JSON line out, per command.
_ECHO_CHILD = '''
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("cmd") == "halt":
        print(json.dumps({"ack": "halt"}), flush=True)
        continue
    if msg.get("cmd") == "tick":
        print(json.dumps({"status": "SUCCESS", "outputs": {"distance_remaining": msg["inputs"].get("goal", "")}}), flush=True)
'''

_HANGING_CHILD = '''
import sys
import time

for line in sys.stdin:
    time.sleep(10)  # never responds within any reasonable test timeout
'''

_CRASHING_CHILD = '''
import sys

for line in sys.stdin:
    sys.exit(1)  # dies instead of responding
'''


def _echo_node(attrs=None, output_keys=("distance_remaining",)):
    node = ProcessBackedNode(
        name="echo", attrs=attrs or {"goal": "dock_A", "distance_remaining": "nav.distance"},
        blackboard={}, command=[sys.executable, "-c", _ECHO_CHILD], output_keys=list(output_keys),
    )
    node.TICK_TIMEOUT_S = 3.0
    node.HALT_TIMEOUT_S = 1.0
    return node


class TestProcessBackedNodeHappyPath:
    def test_a_real_tick_resolves_inputs_and_writes_outputs(self):
        node = _echo_node()
        node.initialise()
        try:
            status = node.update()
            assert status == common.Status.SUCCESS
            assert node.blackboard == {"nav.distance": "dock_A"}
        finally:
            node.terminate(common.Status.SUCCESS)

    def test_input_placeholders_resolve_against_the_blackboard_before_being_sent(self):
        # resolve_attrs' own placeholder regex only matches a plain
        # identifier inside "{...}" (no dots) -- a dotted key like
        # "nav.distance" is only ever valid on the OUTPUT side (write_output
        # takes it as a literal target, no regex constraint there).
        node = ProcessBackedNode(
            name="echo", attrs={"goal": "{mission_goal}", "distance_remaining": "nav.distance"},
            blackboard={"mission_goal": "dock_B"}, command=[sys.executable, "-c", _ECHO_CHILD],
            output_keys=["distance_remaining"],
        )
        node.initialise()
        try:
            node.update()
            assert node.blackboard["nav.distance"] == "dock_B"
        finally:
            node.terminate(common.Status.SUCCESS)

    def test_terminate_sends_halt_and_the_process_exits(self):
        node = _echo_node()
        node.initialise()
        node.update()
        node.terminate(common.Status.SUCCESS)
        assert node._proc is None

    def test_a_process_survives_across_multiple_ticks_true_persistence(self):
        """The whole point of the persistent-process model -- confirms
        the SAME child handles more than one tick, not a fresh spawn
        each time (that's Bash's own, deliberately different, model)."""
        node = _echo_node()
        node.initialise()
        try:
            node.update()
            pid_after_first_tick = node._proc.pid
            node.update()
            assert node._proc.pid == pid_after_first_tick
        finally:
            node.terminate(common.Status.SUCCESS)


class TestProcessBackedNodeFailureModes:
    def test_a_hung_child_times_out_as_failure_and_gets_killed(self):
        node = ProcessBackedNode(
            name="hangs", attrs={}, blackboard={}, command=[sys.executable, "-c", _HANGING_CHILD],
        )
        node.TICK_TIMEOUT_S = 0.3
        node.initialise()
        try:
            status = node.update()
            assert status == common.Status.FAILURE
            assert node._proc is None  # _kill() already ran as part of the timeout path
        finally:
            node.terminate(common.Status.SUCCESS)  # must not raise even though there's nothing left alive

    def test_a_child_that_exits_instead_of_responding_reports_failure_not_an_exception(self):
        node = ProcessBackedNode(
            name="crashes", attrs={}, blackboard={}, command=[sys.executable, "-c", _CRASHING_CHILD],
        )
        node.TICK_TIMEOUT_S = 2.0
        node.initialise()
        try:
            status = node.update()  # must not raise
            assert status == common.Status.FAILURE
        finally:
            node.terminate(common.Status.SUCCESS)

    def test_ticking_after_the_process_already_died_is_a_controlled_failure(self):
        node = ProcessBackedNode(
            name="crashes", attrs={}, blackboard={}, command=[sys.executable, "-c", "pass"],  # exits immediately
        )
        node.initialise()
        node._proc.wait(timeout=2)
        try:
            status = node.update()
            assert status == common.Status.FAILURE
        finally:
            node.terminate(common.Status.SUCCESS)
