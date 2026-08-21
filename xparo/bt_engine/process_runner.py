"""Multi-language custom BT node system, Phase 6/7: the shared runtime for
every OUT-OF-PROCESS language (JavaScript, C++ -- not Bash, see
BashProcessNode's own docstring for why that one is spawn-per-tick
instead). Python nodes run in-process (plugin_loader.CustomBTNode);
everything else runs as its own OS process, one process per node
INSTANCE, spawned once and ticked many times via a tiny JSON-lines
protocol over stdin/stdout -- matching section 15's "process isolation"
requirement and, just as importantly, keeping this safe under Phase 8
parallel execution for free: each ProcessBackedNode instance owns its own
Popen handle, there is no shared mutable process-table state between
instances, so N nodes (any mix of languages) ticking inside the same
Parallel composite need nothing beyond what py_trees already does today.

Wire protocol (deliberately minimal -- this is the "XPARO protocol" the
engine talks in, not something a user ever authors by hand):

    Python -> child, one JSON object + "\\n" on stdin:
        {"cmd": "tick", "inputs": {<resolved input values>}}
        {"cmd": "halt"}

    child -> Python, one JSON object + "\\n" on stdout:
        {"status": "SUCCESS"|"FAILURE"|"RUNNING", "outputs": {<key: value>}}
        {"status": "FAILURE", "error": "<message>"}   (tick raised)
        {"ack": "halt"}

stdout is reserved STRICTLY for that one response line per command --
anything a node wants to log goes to stderr instead (the same split
RUN_COMMAND already uses between real output and diagnostic noise),
avoiding any need to disambiguate "is this stdout line the protocol
response or a debug print" framing.

This module doesn't know or care whether the child happens to be node.js,
a compiled C++ binary, or (in principle) any other future out-of-process
language runner -- CppNode/JavaScriptNode below are just ProcessBackedNode
with a specific `command` to spawn, per section 17's adapter/runner
architecture ("adding a language later means writing a runner, not
rewriting the engine").
"""
import json
import queue
import subprocess
import threading

import py_trees
from py_trees import common

from .nodes.base import resolve_attrs, write_output


class ProcessBackedNode(py_trees.behaviour.Behaviour):
    """Base for a node whose real logic runs in a separate, persistent OS
    process. Subclasses (or direct instantiation via NODE_REGISTRY
    factories -- see engine.py's sync_custom_node_files) only need to
    supply `command` (an argv list) at construction time; everything else
    -- spawn timing, the tick/halt wire protocol, timeout handling,
    process cleanup -- is common across every out-of-process language.

    A crashed/hung/misbehaving child becomes a controlled FAILURE here,
    the same posture plugin_loader.py's _crash_isolated_update established
    for in-process Python plugins -- this repo has already tried and
    rejected "spawn a Popen and just assume it's alive" once (see
    rosbag_control.py's own module docstring for why blackbox_manager's
    old subprocess wrapper was replaced) -- so every read is timeout-
    bounded and every write checks the process is still alive first,
    rather than trusting a handle that might already be a zombie.
    """

    TICK_TIMEOUT_S = 5.0
    HALT_TIMEOUT_S = 2.0

    def __init__(self, name, attrs, blackboard, command, output_keys=(), env=None, ros_node=None):
        super().__init__(name=name)
        self.attrs = attrs
        self.blackboard = blackboard
        self.ros_node = ros_node
        self.command = command
        # None means "inherit this process' own environment as-is" (the
        # subprocess default) -- only the compiled C++ runner overrides
        # this today, to defensively extend LD_LIBRARY_PATH rather than
        # assume the parent process was launched from an already-sourced
        # ROS environment (see runners.py's make_cpp_node_factory).
        self.env = env
        # The raw XML attrs dict (self.attrs) mixes input AND output port
        # names with no direction marker of its own -- that distinction
        # only exists in Django's NodePort metadata, so whichever factory
        # constructs this (see runners.py) passes the output port keys
        # through explicitly. JavaScript doesn't strictly need this (its
        # own this.output() calls already say exactly which keys were
        # written); a compiled-language host that has to read specific
        # blackboard keys back after tick does.
        self.output_keys = list(output_keys)
        self._proc = None
        self._out_queue = None
        self._reader_thread = None

    def _spawn(self):
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.env,
        )
        self._out_queue = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        # Runs in its own thread for the process' whole lifetime -- a
        # plain blocking readline() has no built-in per-call timeout in
        # Python, so update()/terminate() below block on queue.get(timeout=...)
        # instead of on the pipe directly.
        try:
            for line in self._proc.stdout:
                self._out_queue.put(line)
        except (ValueError, OSError):
            pass  # pipe closed out from under us during shutdown -- fine

    def initialise(self):
        self._spawn()

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def update(self):
        if not self._alive():
            self.feedback_message = "child process is not running"
            return common.Status.FAILURE

        try:
            resolved = resolve_attrs(self.attrs, self.blackboard)
        except Exception as exc:
            self.feedback_message = str(exc)
            return common.Status.FAILURE

        try:
            self._proc.stdin.write(
                json.dumps({"cmd": "tick", "inputs": resolved, "output_keys": self.output_keys}) + "\n"
            )
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.feedback_message = f"failed to send tick: {exc}"
            return common.Status.FAILURE

        try:
            line = self._out_queue.get(timeout=self.TICK_TIMEOUT_S)
        except queue.Empty:
            self.feedback_message = f"no response within {self.TICK_TIMEOUT_S}s -- treating as hung"
            self._kill()
            return common.Status.FAILURE

        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            self.feedback_message = f"malformed response: {exc}"
            return common.Status.FAILURE

        if "error" in response:
            self.feedback_message = str(response["error"])

        status_name = response.get("status", "FAILURE")
        for key, value in (response.get("outputs") or {}).items():
            write_output(self.attrs, self.blackboard, key, value)

        return getattr(common.Status, status_name, common.Status.FAILURE)

    def terminate(self, new_status):
        if self._alive():
            try:
                self._proc.stdin.write(json.dumps({"cmd": "halt"}) + "\n")
                self._proc.stdin.flush()
                self._out_queue.get(timeout=self.HALT_TIMEOUT_S)
            except (BrokenPipeError, OSError, queue.Empty):
                pass  # best-effort ack -- terminate below is the real guarantee
        self._kill()

    def _kill(self):
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=self.HALT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=self.HALT_TIMEOUT_S)
        for stream in (self._proc.stdin, self._proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        self._proc = None
