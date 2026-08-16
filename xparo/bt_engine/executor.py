"""Behaviour Tree redesign Phase 9: BehaviorTreeExecutor -- builds a
py_trees tree from a stored fragment + an already-resolved blackboard
dict, ticks it to completion, and relays every real status change to the
dashboard's existing live-tree visualization in the exact shape
Xparo.navigation_callback (xparo_ros.py) already produces from Nav2's
BehaviorTreeLog, so that consumer needs zero changes to also show locally-
executed trees.

Composable class owned by the Xparo rclpy node, not its own Node --
mirrors rosbag_control.py's RosbagControl exactly (constructed with a
reference to the host node plus whatever else it needs, using the node's
own create_client/publisher/timer rather than spinning up a second node).
`engine` is the already-constructed Engine instance (xparo_ros.py builds
this after Engine, same ordering RosbagControl doesn't need but this does,
since it must call engine.add_live_update).

Deliberately uses a plain Python dict as the blackboard, not py_trees' own
Blackboard/Client system -- that's a process-wide global keyed by string
paths with its own key-registration ceremony, which would need careful
per-run namespacing to avoid one task's variables leaking into a
concurrently-running second task's tree on the same robot process. A
fresh dict per run() call is simpler and fully isolated by construction.

run() is synchronous and blocks the calling thread until the tree
reaches SUCCESS/FAILURE or max_ticks is hit -- it does not spawn its own
thread. Phase 11's RUN_TASK handler is the one that needs this off the
WS-dispatch thread, and does so the same way remote_ops.py's RUN_COMMAND
already does (threading.Thread(target=...)), rather than this class
managing its own thread and hiding that decision from callers that might
want it synchronous (as every test in test_bt_engine.py does).
"""
import time
from datetime import datetime

import py_trees
from py_trees import common

from . import tree_builder


class BehaviorTreeExecutor:
    def __init__(self, node, engine):
        self.node = node
        self.engine = engine

    def run(self, xml_fragment, blackboard=None, tick_rate_hz=10, max_ticks=1000):
        """Returns (final_status, blackboard) -- the caller (Phase 11) needs
        the post-run blackboard back (e.g. to report resolved values in
        TASK_RESULT), not just the status.
        """
        blackboard = {} if blackboard is None else blackboard
        root = tree_builder.build_tree(xml_fragment, blackboard, ros_node=self.node)
        tree = py_trees.trees.BehaviourTree(root)

        previous_status = {}

        def _on_post_tick(ticked_tree):
            # root.iterate() walks the *whole* tree structure, including
            # siblings never actually reached this tick (e.g. later steps
            # in a Sequence that never got past an earlier RUNNING one) --
            # those sit at py_trees' own default Status.INVALID, same as
            # "never seen before". Defaulting the lookup to INVALID (not
            # None) makes that comparison correctly a no-op instead of a
            # spurious "None -> INVALID" event on every node that merely
            # exists in the tree, not just the ones actually ticked.
            for behaviour in ticked_tree.root.iterate():
                prev = previous_status.get(behaviour.id, common.Status.INVALID)
                curr = behaviour.status
                if prev != curr:
                    self._emit_live_update(behaviour, prev, curr)
                    previous_status[behaviour.id] = curr

        tree.add_post_tick_handler(_on_post_tick)

        interval = (1.0 / tick_rate_hz) if tick_rate_hz else 0
        for _ in range(max_ticks):
            tree.tick()
            if root.status in (common.Status.SUCCESS, common.Status.FAILURE):
                break
            if interval:
                time.sleep(interval)

        return root.status, blackboard

    def _emit_live_update(self, behaviour, prev_status, curr_status):
        now = time.time()
        data = {
            "node_name": behaviour.name,
            # The registration/type name (tree_builder.py sets
            # node.xparo_tag to the XML tag at build time), distinct from
            # node_name -- mirrors this project's own prior C++
            # RosTopicLogger exactly (node.name() vs
            # node.registrationName()). xparo_ros.py's navigation_callback
            # (Nav2's forwarded BehaviorTreeLog) still duplicates node_name
            # into this field -- that's Nav2's own message schema
            # genuinely having no separate field, not a choice made here.
            "node_type": getattr(behaviour, "xparo_tag", behaviour.name),
            "uid": str(behaviour.id),
            "prev": prev_status.name,
            "curr": curr_status.name,
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.engine.add_live_update(data)
