"""Multi-language custom BT node system, Phase 6/7: the three non-Python
runners -- BashProcessNode (spawn-per-tick), and the JavaScript/C++
factories built on process_runner.ProcessBackedNode (persistent process).
Registered into NODE_REGISTRY directly by engine.py's
sync_custom_node_files (not through plugin_loader.load_plugins' class-
scanning, which is Python-source-specific) -- see that method's own
docstring for the registration flow.
"""
import os
import re
import subprocess

from py_trees import behaviour, common

from .nodes.base import resolve_attrs, write_output
from .process_runner import ProcessBackedNode

# ---------------------------------------------------------------------------
# Bash -- deliberately NOT a ProcessBackedNode. The template this protocol
# was already generated against (apps/analytics/custom_node_files.py's
# _bash_template, outer repo) is a one-shot script: inputs as upper-cased
# env vars, outputs as "KEY=value" stdout lines, status as the exit code
# (0=SUCCESS, 1=FAILURE, 2=RUNNING) -- there's no persistent process for a
# RUNNING bash node to stay alive *as*, each tick is its own fresh
# subprocess.run call, exactly matching remote_ops.py's own established
# RUN_COMMAND pattern (blocking, timeout-bounded, never inline on the
# dispatch thread -- callers reach this only via the same background-
# thread chain RUN_COMMAND/RUN_TASK already use). A RUNNING bash node has
# no live child to HALT by the time halt() is even reachable -- the
# previous tick's process has already exited by then -- so halt() is a
# genuine no-op here, not a corner cut.
# ---------------------------------------------------------------------------


class BashProcessNode(behaviour.Behaviour):
    TICK_TIMEOUT_S = 5.0
    _STATUS_BY_EXIT_CODE = {0: common.Status.SUCCESS, 1: common.Status.FAILURE, 2: common.Status.RUNNING}

    def __init__(self, name, attrs, blackboard, script_path, ros_node=None):
        super().__init__(name=name)
        self.attrs = attrs
        self.blackboard = blackboard
        self.ros_node = ros_node
        self.script_path = script_path

    def update(self):
        try:
            resolved = resolve_attrs(self.attrs, self.blackboard)
        except Exception as exc:
            self.feedback_message = str(exc)
            return common.Status.FAILURE

        env = dict(os.environ)
        for key, value in resolved.items():
            env[key.upper()] = "" if value is None else str(value)

        try:
            proc = subprocess.run(
                [self.script_path], env=env, capture_output=True, text=True, timeout=self.TICK_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            self.feedback_message = f"no exit within {self.TICK_TIMEOUT_S}s -- treating as hung"
            return common.Status.FAILURE
        except OSError as exc:
            self.feedback_message = f"failed to run: {exc}"
            return common.Status.FAILURE

        for line in proc.stdout.splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            write_output(self.attrs, self.blackboard, key.strip().lower(), value.strip())

        status = self._STATUS_BY_EXIT_CODE.get(proc.returncode)
        if status is None:
            self.feedback_message = f"exit code {proc.returncode} is outside 0/1/2: {proc.stderr.strip()[:200]}"
            return common.Status.FAILURE
        return status

    def halt(self):
        pass  # see class docstring above -- no live child by the time this runs


# ---------------------------------------------------------------------------
# JavaScript -- a persistent `node js_host.js <user_file>` process per node
# instance (ProcessBackedNode). js_host.js requires the user's file exactly
# as `module.exports = <Class>` already shapes it (apps/analytics/
# custom_node_files.py's _javascript_template, unchanged), instantiates it
# ONCE, then answers "tick"/"halt" commands on stdin for that instance's
# whole lifetime -- so a RUNNING node genuinely keeps its own JS-side state
# between ticks, unlike Bash's stateless-per-tick model above. XparoNode
# (the base class the template `extends`) is injected as a Node.js global
# before the user file is required, since the template itself never writes
# `require("xparo_node")` -- these two files are written once per robot,
# not per node, by ensure_js_runtime below.
# ---------------------------------------------------------------------------

XPARO_NODE_JS = '''"use strict";
// The JavaScript-side mirror of xparo/bt_engine/nodes/base.py's
// resolve_attrs/write_output pair -- input()/output() work purely on the
// per-tick inputs dict js_host.js hands this instance, and the outputs
// dict js_host.js reads back after tick() returns. No blackboard access
// happens in this process at all -- that's the Python host's job, exactly
// like every other language here (section 7's own "node accesses only the
// values it's been given" principle).
class XparoNode {
  constructor() {
    this.SUCCESS = "SUCCESS";
    this.RUNNING = "RUNNING";
    this.FAILURE = "FAILURE";
    this._inputs = {};
    this._outputs = {};
  }

  input(key, defaultValue) {
    return Object.prototype.hasOwnProperty.call(this._inputs, key) ? this._inputs[key] : defaultValue;
  }

  output(key, value) {
    this._outputs[key] = value;
  }

  halt() {}
}

module.exports = XparoNode;
'''

JS_HOST_JS = '''#!/usr/bin/env node
"use strict";
// Multi-language custom BT node system, Phase 7 -- the Node.js half of
// process_runner.ProcessBackedNode's wire protocol (see that module's own
// docstring for the exact JSON shape). Spawned once per node instance by
// runners.py's make_javascript_node_factory; stays alive for that
// instance's whole lifetime, answering one "tick"/"halt" command per
// stdin line with one JSON response per stdout line. stdout is reserved
// strictly for that response -- anything the user's own code wants to log
// should go to console.error (stderr), never console.log.
const readline = require("readline");
const path = require("path");

global.XparoNode = require(path.join(__dirname, "xparo_node.js"));

const userModulePath = process.argv[2];
const UserNodeClass = require(path.resolve(userModulePath));
const instance = new UserNodeClass();

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", (line) => {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (e) {
    process.stdout.write(JSON.stringify({ status: "FAILURE", error: "invalid command JSON: " + e.message }) + "\\n");
    return;
  }

  if (msg.cmd === "halt") {
    try {
      if (typeof instance.halt === "function") instance.halt();
    } catch (e) {
      // best-effort -- the ack below is what the Python side actually
      // waits on, a broken halt() must not hang the shutdown.
    }
    process.stdout.write(JSON.stringify({ ack: "halt" }) + "\\n");
    return;
  }

  if (msg.cmd !== "tick") return;

  instance._inputs = msg.inputs || {};
  instance._outputs = {};
  let status;
  try {
    status = instance.tick();
  } catch (e) {
    process.stdout.write(JSON.stringify({ status: "FAILURE", error: String((e && e.message) || e) }) + "\\n");
    return;
  }
  process.stdout.write(JSON.stringify({ status: status, outputs: instance._outputs }) + "\\n");
});
'''


def ensure_js_runtime(runtime_dir):
    """Writes xparo_node.js/js_host.js to `runtime_dir` if they're not
    already there with current content -- idempotent (safe to call on
    every sync, not just the first) and shared by every JavaScript node
    in the project, unlike the per-file .js sources next to them (each
    node gets its own file; the runtime is written once)."""
    os.makedirs(runtime_dir, exist_ok=True)
    for filename, content in (("xparo_node.js", XPARO_NODE_JS), ("js_host.js", JS_HOST_JS)):
        path_ = os.path.join(runtime_dir, filename)
        if not os.path.exists(path_) or open(path_).read() != content:
            with open(path_, "w") as file:
                file.write(content)


def make_javascript_node_factory(source_path, runtime_dir, output_keys):
    """Returns a NODE_REGISTRY-shaped (name, attrs, blackboard, children,
    ros_node) -> Behaviour builder for a single JavaScript CustomFile.
    `runtime_dir` must already have ensure_js_runtime() run against it --
    callers (engine.py's sync_custom_node_files) do that once per sync,
    not once per node, since it's shared."""
    host_path = os.path.join(runtime_dir, "js_host.js")

    def builder(name, attrs, blackboard, children, ros_node):
        return ProcessBackedNode(
            name=name, attrs=attrs, blackboard=blackboard,
            command=["node", host_path, source_path],
            output_keys=output_keys, ros_node=ros_node,
        )

    return builder


# ---------------------------------------------------------------------------
# C++ -- compiled once per sync (not per tick) into a standalone executable
# that embeds BOTH the user's real BT::SyncActionNode/BT::ConditionNode
# subclass (apps/analytics/custom_node_files.py's _cpp_template, unchanged
# -- verified against the real, installed behaviortree_cpp 4.9.0 headers
# before writing this) AND a small fixed main() this module generates,
# reading the exact same JSON-lines protocol every other out-of-process
# language here speaks. Real BT::NodeConfig/Blackboard construction (no
# XML/tree needed to tick a single node standalone -- confirmed by direct
# compilation+execution against the real library, not assumed from docs).
# ---------------------------------------------------------------------------

_CPP_CLASS_RE = re.compile(r"class\s+(\w+)\s*:\s*public\s+BT::(?:SyncActionNode|ConditionNode)")

# Real ROS2 installs always set ROS_DISTRO once the workspace's setup.bash
# has been sourced (confirmed live in this exact sandbox: jazzy, despite
# README.md's stale "humble" claim) -- deriving the include/lib path from
# it, rather than hardcoding one distro, is the difference between this
# working only in dev and working on whatever distro a real robot runs.
_ROS_DISTRO = os.environ.get("ROS_DISTRO", "jazzy")
_ROS_INCLUDE_DIR = f"/opt/ros/{_ROS_DISTRO}/include"
_ROS_LIB_DIR = f"/opt/ros/{_ROS_DISTRO}/lib"

_CPP_HOST_MAIN = '''
#include <nlohmann/json.hpp>
#include <iostream>
#include <string>
#include <vector>

using XparoNodeClass = {class_name};

int main()
{{
    std::string line;
    while (std::getline(std::cin, line)) {{
        if (line.empty()) continue;

        nlohmann::json msg;
        try {{
            msg = nlohmann::json::parse(line);
        }} catch (...) {{
            nlohmann::json resp;
            resp["status"] = "FAILURE";
            resp["error"] = "invalid command JSON";
            std::cout << resp.dump() << std::endl;
            continue;
        }}

        std::string cmd = msg.value("cmd", "");
        if (cmd == "halt") {{
            // No instance persists between ticks (see class docstring in
            // runners.py) -- nothing to actually call halt() on here.
            nlohmann::json resp;
            resp["ack"] = "halt";
            std::cout << resp.dump() << std::endl;
            continue;
        }}
        if (cmd != "tick") continue;

        BT::NodeConfig config;
        auto bb = BT::Blackboard::create();
        config.blackboard = bb;

        if (msg.contains("inputs")) {{
            for (auto it = msg["inputs"].begin(); it != msg["inputs"].end(); ++it) {{
                config.input_ports[it.key()] = it.value().is_string()
                    ? it.value().get<std::string>() : it.value().dump();
            }}
        }}

        std::vector<std::string> output_keys;
        if (msg.contains("output_keys")) {{
            for (auto& k : msg["output_keys"]) {{
                std::string key = k.get<std::string>();
                output_keys.push_back(key);
                config.output_ports[key] = "{{" + key + "}}";
            }}
        }}

        nlohmann::json resp;
        try {{
            XparoNodeClass node("cpp_node", config);
            BT::NodeStatus status = node.executeTick();
            resp["status"] = BT::toStr(status);
        }} catch (const std::exception& e) {{
            resp["status"] = "FAILURE";
            resp["error"] = e.what();
            std::cout << resp.dump() << std::endl;
            continue;
        }}

        nlohmann::json outputs = nlohmann::json::object();
        for (auto& key : output_keys) {{
            std::string value;
            if (bb->get(key, value)) outputs[key] = value;
        }}
        resp["outputs"] = outputs;
        std::cout << resp.dump() << std::endl;
    }}
    return 0;
}}
'''


def compile_cpp_node(source, header_source, build_dir, node_name):
    """Compiles `source` (a real BT::SyncActionNode/BT::ConditionNode
    subclass, as-is -- see module docstring) plus this module's own fixed
    host main() into one standalone executable under `build_dir`. Returns
    the executable path on success, None on any failure (a bad compile
    contributes nothing and is logged, never raised -- matching
    plugin_loader.load_plugins' own "one bad file doesn't take down the
    sync" posture). `header_source` is written alongside as
    <node_name>.hpp and #include-d automatically if non-empty (matching
    CustomNodeDefinition's own header_source field) -- real dependency/
    build-flag support (extra find_package/target_link_libraries per
    node) is intentionally NOT attempted here; see this function's return
    value/caller for how a node needing more than behaviortree_cpp itself
    degrades (skipped, not silently pretended to work).
    """
    match = _CPP_CLASS_RE.search(source)
    if not match:
        print(f"[bt_engine cpp runner] {node_name}: no class extending "
              f"BT::SyncActionNode/BT::ConditionNode found -- not building")
        return None
    class_name = match.group(1)

    os.makedirs(build_dir, exist_ok=True)
    header_path = os.path.join(build_dir, f"{node_name}.hpp")
    source_path = os.path.join(build_dir, f"{node_name}.cpp")
    executable_path = os.path.join(build_dir, node_name)

    header_include = ""
    if header_source.strip():
        with open(header_path, "w") as file:
            file.write(header_source)
        header_include = f'#include "{node_name}.hpp"\n'
    elif os.path.exists(header_path):
        os.remove(header_path)

    with open(source_path, "w") as file:
        file.write(header_include + source + _CPP_HOST_MAIN.format(class_name=class_name))

    compile_cmd = [
        "g++", "-std=c++17", source_path,
        "-I", _ROS_INCLUDE_DIR,
        "-L", _ROS_LIB_DIR, "-lbehaviortree_cpp",
        "-o", executable_path,
    ]
    try:
        result = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[bt_engine cpp runner] {node_name}: compile failed to run: {exc}")
        return None
    if result.returncode != 0:
        print(f"[bt_engine cpp runner] {node_name}: compile failed:\\n{result.stderr[-2000:]}")
        return None
    return executable_path


def make_cpp_node_factory(executable_path, output_keys):
    """Same NODE_REGISTRY-shaped builder as make_javascript_node_factory,
    just spawning the already-compiled executable directly (no host
    script argv needed -- the host main() is baked into the binary
    itself)."""
    # A real deployment normally already has this set (ros2 run only works
    # from an already-sourced ROS environment in the first place) -- set
    # defensively anyway rather than assume that's always true of whatever
    # process actually launched xparo_ros.
    env = dict(os.environ)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{_ROS_LIB_DIR}:{existing}" if existing else _ROS_LIB_DIR

    def builder(name, attrs, blackboard, children, ros_node):
        return ProcessBackedNode(
            name=name, attrs=attrs, blackboard=blackboard,
            command=[executable_path], env=env,
            output_keys=output_keys, ros_node=ros_node,
        )

    return builder
