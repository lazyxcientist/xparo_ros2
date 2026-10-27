#!/usr/bin/env python3
import sys

# print() throughout this process (here, engine.py, database.py,
# transports/django_ws.py, ...) is how an operator running via `ros2 run`/
# `ros2 launch` sees live connection status -- but Python fully
# block-buffers stdout whenever it isn't a TTY, which is exactly what both
# of those wrap it in (a pipe, to prefix each line with the node name).
# Without this, real, successful output (connection banner, credential
# issuance, synced project data) can sit invisible in the buffer for a
# very long time, looking indistinguishable from a genuine hang -- this is
# not hypothetical, it's exactly what made a real ros2 launch run look
# stuck for tens of seconds despite the node having already connected
# successfully. Reconfiguring here, once, at the real process entry point,
# fixes it process-wide regardless of which module the print() is in.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String
from sensor_msgs.msg import Joy
from ament_index_python.packages import get_package_share_directory
import json
import os
import time
import yaml
from datetime import datetime


from xparo.engine import Engine
from xparo.task_call import TaskCallOffer
from xparo.project_files import retire_other_project_files
from xparo.bt_engine import run_task
from xparo.rosbag_control import RosbagControl, load_rosbag_config
from xparo.diagnostics_aggregator import DiagnosticsAggregator
from xparo.rosout_watcher import RosoutWatcher
from xparo.health import ProblemTracker
from xparo.engine import HEALTH_FLUSH_SEC
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus

SELF_DIAGNOSTICS_PERIOD_SEC = 1.0
_DIAG_LEVELS = {'ok': DiagnosticStatus.OK, 'warn': DiagnosticStatus.WARN,
                'error': DiagnosticStatus.ERROR, 'stale': DiagnosticStatus.STALE}
from xparo.bt_engine.executor import BehaviorTreeExecutor

try:
    from nav2_msgs.msg import BehaviorTreeLog
    NAV2_AVAILABLE = True
except ImportError:
    print("[WARN] nav2_msgs not available. Subscriber will not start.")
    NAV2_AVAILABLE = False



base_path_for_package = get_package_share_directory('xparo')


class Xparo(Node):
    def __init__(self):
        super().__init__("xparo")
        ################################################
        ########## for production purpose only #########
        try:
            with open(os.path.expanduser("~/xparo_config.json"), 'r') as file:
              xparo_config = json.load(file)
            self.get_logger().warning("ALERT: xparo_config.json file found in home directory. Using it for configuration.")
            self.get_logger().warning("we you are not in production mode. then delete the ~/xparo_config.json file.")
        except:
          xparo_config={}
        # xparo_config.get("", "")
        ################################################
        ## parameters
        # Pasted keys often carry spaces, quotes or a newline: those used to
        # end up in the connection URL and fail with a misleading
        # "check your internet connection" loop.
        # An empty parameter means "not set": ros2 launch always passes ""
        # for these, which used to hide ~/xparo_config.json completely.
        self.xparo_project_id = clean_setting(self.declare_parameter("xparo_project_id", xparo_config.get("xparo_project_id", "")  ).value) \
            or clean_setting(xparo_config.get("xparo_project_id", ""))
        self.xparo_secret_key = clean_setting(self.declare_parameter("xparo_secret_key", xparo_config.get("xparo_secret_key", "") ).value) \
            or clean_setting(xparo_config.get("xparo_secret_key", ""))
        for problem in check_connection_settings(self.xparo_project_id, self.xparo_secret_key):
            self.get_logger().error(problem)

        self.xparo_connection_type = self.declare_parameter("xparo_connection_type", xparo_config.get("xparo_connection_type", "websocket")  ).value
        # "production" (xparo.in, https/wss) or "local" (127.0.0.1:8000,
        # http/ws, for developing against a local Django server). Defaults
        # to production -- a real robot that never sets this now reaches
        # the real server instead of a dev sandbox that doesn't exist for
        # it (see transports/django_ws.py's DEFAULT_ENVIRONMENT for the old
        # hardcoded `local = True` this replaces).
        self.xparo_environment = self.declare_parameter("xparo_environment", xparo_config.get("xparo_environment", "production")  ).value
        # Which Task stages (apps/analytics/models.py's TaskStageChoices,
        # outer repo) this robot may run a RUN_TASK dispatch for -- see
        # bt_engine/run_task.py's ALLOWED_TASK_STAGES for the actual
        # cascade. Defaults to "production", matching xparo_environment's
        # own safety-first default just above.
        self.xparo_stage = self.declare_parameter("xparo_stage", xparo_config.get("xparo_stage", "production")  ).value
        self.xparo_folder = self.declare_parameter("xparo_folder", base_path_for_package  ).value
        for problem in check_run_environment(self.xparo_folder):
            self.get_logger().warning(problem)
        # One xparo per project on a robot: a second copy (started by
        # accident) fought the first for the server connection and ran
        # every topic-triggered task twice.
        self._instance_lock_path = os.path.join(
            os.environ.get("XPARO_STATE_DIR") or os.path.join(self.xparo_folder, "xparo", self.xparo_project_id or "no-project"),
            "xparo.lock")
        self._instance_lock = acquire_single_instance_lock(self._instance_lock_path)
        if self._instance_lock is None:
            # Wait instead of exiting: launch's respawn=True restarted an
            # exiting copy every 3 s forever, filling the logs. This one
            # takes over if the other stops.
            self.get_logger().error("Another xparo node is already running for this project on this robot. "
                                    "This copy waits and takes over if that one stops "
                                    "(to stop it: ros2 node list / ps aux | grep xparo).")
            self._instance_lock = wait_for_instance_lock(self._instance_lock_path)
        self.xparo_behavior_path = self.declare_parameter("xparo_behavior_path",os.path.join(self.xparo_folder,'config','default.xml')).value
        self.xparo_file_path = self.declare_parameter("xparo_file_path",os.path.join(self.xparo_folder,'config','default.txt')).value
        self.xparo_env_path = self.declare_parameter("xparo_env_path",os.path.join(self.xparo_folder,'config','default.env')).value
        self.xparo_local_env_path = self.declare_parameter("xparo_local_env_path",os.path.join(self.xparo_folder,'config','local.env')).value
        self.xparo_properties_path = self.declare_parameter("xparo_properties_path",os.path.join(self.xparo_folder,'config',"properties.txt")).value
        self.xparo_custom_behaviors_folder_path = self.declare_parameter("xparo_custom_behaviors_folder_path",os.path.join(self.xparo_folder,'custom_behaviors')).value
        self.xparo_custom_files_folder_path = self.declare_parameter("xparo_custom_files_folder_path",os.path.join(self.xparo_folder,'custom_files')).value
        self.xparo_custom_evns_folder_path = self.declare_parameter("xparo_custom_evns_folder_path",os.path.join(self.xparo_folder,'custom_envs')).value
        self.record_bags = self.declare_parameter("record_bags",False).value
        self.BAG_DIR = self.declare_parameter("BAG_DIR",os.path.join(self.xparo_folder,"xparo",self.xparo_project_id,'ros_bags')).value
        # "django_ws" (networked robots, against the Django backend -- the
        # only option before Phase 4) or "tethered_tcp" (a physically-
        # tethered ROV with no path to Django at all). See
        # transports/tethered_tcp.py's module docstring.
        self.xparo_transport = self.declare_parameter("xparo_transport", xparo_config.get("xparo_transport", "django_ws")).value
        # Only read/relevant when xparo_transport=="tethered_tcp" -- path
        # to a copy of config/tethered_channels.yaml with this
        # deployment's actual RS-485<->Ethernet bridge host/port pairs.
        # Empty (default) means transports/tethered_tcp.py's own
        # DEFAULT_CHANNELS_CONFIG (loopback test values, not a real link).
        self.tethered_channels_config_path = self.declare_parameter(
            "tethered_channels_config_path", xparo_config.get("tethered_channels_config_path", "")
        ).value
        self.tethered_channels_config = self._load_tethered_channels_config(self.tethered_channels_config_path)
        # Ads Center: who draws this robot's ads on its screen -- "native"
        # (xparo's own always-on-top player, for robots without XP-shell),
        # "xpshell" (hand them to XP-shell's Kivy player over ROS 2) or
        # "off". See xparo/ads/backends/.
        self.xparo_ads_display = self.declare_parameter(
            "xparo_ads_display", xparo_config.get("xparo_ads_display", "auto")
        ).value


        self.ask_question = self.create_subscription(String, '/xparo/ask', self._guarded(self.ask_question_fun), 10)
        self.task_updates = self.create_subscription(String,'/xparo/task_updates',self._guarded(self.task_updates),10)
        self.bt_log = self.create_subscription(String,'/bt_xparo_log',self._guarded(self.live_updates),10)
        # Behaviour Tree redesign: local task execution -- publish
        # {"task_id": "...", "override_params": {...}} here (override_params
        # optional) to run an already-synced task (Engine.sync_custom_tasks)
        # directly on this robot, no Django round trip needed at trigger
        # time. Same naming convention as /xparo/ask, /xparo/task_updates.
        self.run_task_sub = self.create_subscription(String, '/xparo/run_task', self._guarded(self.run_task_topic_cb), 10)
        self.dash_send = self.create_publisher(String,"/xparo/response",10)
        # AI task calling (xparo/task_call.py): ask in plain words on
        # /xparo/ask_task; the task the AI picks (by its AI prompt) is
        # offered on /xparo/task_call and runs on "call" sent to
        # /xparo/task_call_confirm within 15 s (no answer: nothing runs);
        # "cancel" drops the offer or stops the run it started.
        self.ask_task_sub = self.create_subscription(String, '/xparo/ask_task', self._guarded(self.ask_task_cb), 10)
        self.task_call_confirm_sub = self.create_subscription(String, '/xparo/task_call_confirm', self._guarded(self.task_call_confirm_cb), 10)
        self.task_call_pub = self.create_publisher(String, '/xparo/task_call', 10)
        # Stop a running task from the robot itself (task id, or {"task_id"/"run_id"}).
        self.cancel_task_sub = self.create_subscription(String, '/xparo/cancel_task', self._guarded(self.cancel_task_cb), 10)
        # Phase 4 TELEOP -- remote_ops.handle_teleop publishes here via
        # Engine's joy_publish callback, below.
        self.joy_pub = self.create_publisher(Joy, '/joy', 10)


        if NAV2_AVAILABLE:
            try:
                self.subscription = self.create_subscription(
                    BehaviorTreeLog,
                    '/behavior_tree_log',
                    self.navigation_callback,
                    10
                )
                self.get_logger().info("Subscribed to /behavior_tree_log")
            except Exception as e:
                self.get_logger().error(f"Failed to subscribe: {e}")


        # files
        self.files = {'behavior'         : self.xparo_behavior_path,
                    'file'              : self.xparo_file_path,
                    'env'         : self.xparo_env_path,
                    'local_env'         : self.xparo_local_env_path,
                    'properties'   : self.xparo_properties_path,
                    'xparo_custom_behaviors_folder_path'   : self.xparo_custom_behaviors_folder_path,
                    'xparo_custom_files_folder_path'   : self.xparo_custom_files_folder_path,
                    'xparo_custom_evns_folder_path'   : self.xparo_custom_evns_folder_path,
                        }
        ####################
        
        # RosbagControl needs a live rclpy node to create clients/timers/etc
        # on (that's `self`, here) -- it can't be built inside Engine/
        # XP_Database/BlackboxOrchestrator, none of which are rclpy-aware
        # (Engine is usable standalone, outside ROS2 entirely -- see its
        # own __main__ block).
        #
        # Always constructed now, regardless of record_bags -- a real,
        # reported bug: record_bags used to gate this object's very
        # existence, so a robot launched without record_bags:=true (this
        # project's own default) had literally nothing watching
        # /rosbag2_recorder's services at all, even for a completely
        # separate `ros2 bag record` process the user started by hand.
        # Confirmed live: `ros2 bag record` exposes the exact same
        # /rosbag2_recorder/{record,stop,resume,is_paused,
        # is_discovery_running} service names regardless of who launches
        # it -- RosbagControl's own service clients were always capable of
        # driving/detecting ANY such recorder, they just never got built.
        # record_bags now only controls owns_launch_process below (whether
        # THIS launch file also started its own recorder subprocess that
        # needs its throwaway boot session force-closed) -- watching/
        # controlling whatever recorder actually exists is unconditional.
        #
        # start_mode/start_delay_seconds come from whatever was persisted
        # by a previous sync (Manage_Dash.py's rosbag_config, relayed via
        # engine.py's sync_rosbag_config) -- read here, before
        # RosbagControl exists, since its __init__ kicks off the boot
        # sequence immediately and can't wait for Engine's own
        # post-construction sync the way BT plugin config does.
        rosbag_config = load_rosbag_config(self.xparo_custom_behaviors_folder_path)
        self.rosbag_control = RosbagControl(
            self, self.BAG_DIR,
            start_mode=rosbag_config['start_mode'],
            start_delay_seconds=rosbag_config['start_delay_seconds'],
            owns_launch_process=self.record_bags,
        )

        # Same ownership pattern as rosbag_control just above -- needs a
        # real Node to subscribe to /diagnostics on, so it's built here,
        # not inside Engine (which is usable standalone, outside ROS2
        # entirely). Engine reaches it the same way it already reaches
        # rosbag_control: getattr(self.bt_executor.node, ..., None).
        # One problem list for the Health & Errors popup, fed by both
        # /diagnostics (every node's status, xparo's own included -- see
        # _publish_self_diagnostics) and /rosout ERROR/FATAL lines.
        # Engine.flush_health sends what changed every HEALTH_FLUSH_SEC.
        self.problem_tracker = ProblemTracker()
        self.diagnostics_aggregator = DiagnosticsAggregator(self, tracker=self.problem_tracker)
        self.rosout_watcher = RosoutWatcher(self, self.problem_tracker)
        self.self_diagnostics_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10)
        self.task_result_pub = self.create_publisher(String, '/xparo/task_result', 10)

        # record_bags and BAG_DIR must go in through the constructor, not be
        # set as post-construction attributes -- Engine.__init__ already
        # builds its XP_Database (and, if record_bags is True, starts the
        # BlackboxOrchestrator against whatever BAG_DIR it was given) before
        # returning, so setting either attribute afterward was a no-op:
        # bags either silently never recorded, or recorded to the wrong path.
        self.xparo_engine = Engine(self.xparo_secret_key,
                              self.xparo_project_id,
                              connection_type = self.xparo_connection_type,
                              record_bags = self.record_bags,
                              BAG_DIR = self.BAG_DIR,
                              environment = self.xparo_environment,
                              xparo_stage = self.xparo_stage,
                              rosbag_control = self.rosbag_control,
                              joy_publish = self._publish_joy,
                              xparo_transport = self.xparo_transport,
                              tethered_channels_config = self.tethered_channels_config)
        self.xparo_engine.call_message=self.call_message
        self.xparo_engine.files = self.files
        # A robot switched to another project must not carry the old
        # project's trees/tasks/code into the new one.
        try:
            moved = retire_other_project_files(self.files["xparo_custom_behaviors_folder_path"],
                                               self.files["xparo_custom_evns_folder_path"], self.xparo_project_id)
            if moved:
                self.get_logger().warning(f"This robot moved to another project: the previous project's synced "
                                          f"files were moved to {moved}")
        except OSError as e:
            self.get_logger().error(f"couldn't set aside the previous project's files: {e}")
        self.xparo_engine.on_task_event = self._on_task_event
        self.xparo_engine.task_call = TaskCallOffer(
            self.xparo_engine.run_task_from_topic, self._publish_task_call,
            cancel_task=lambda task_id: run_task.cancel_task(task_id=task_id, reason="cancelled on the robot (/xparo/task_call_confirm)"))
        # OTA updates (ota.py) end by restarting the node with the new code.
        self.restart_requested = False
        self.xparo_engine.restart_process = self._request_restart
        # Composable, owned by this node like self.rosbag_control above --
        # unlike that one, it needs the already-built xparo_engine (to call
        # add_live_update/add_task_history on), so it can only be
        # constructed here, after Engine exists, not passed into Engine's
        # own constructor the way rosbag_control is.
        self.bt_executor = BehaviorTreeExecutor(self, self.xparo_engine)
        self.xparo_engine.bt_executor = self.bt_executor
        # Load whatever plugin paths were persisted from a previous sync
        # (Phase 12) -- before connect(), so a RUN_TASK referencing a
        # plugin tag can never race a fresh boot that hasn't loaded it yet.
        self.xparo_engine.sync_bt_plugins()
        # Same reasoning as sync_bt_plugins just above, for Phase 13's
        # inline-authored nodes -- persisted per-node .py files under
        # custom_behaviors/inline_nodes/ from a previous sync.
        self.xparo_engine.sync_bt_inline_nodes()
        # Same reasoning again, for the multi-language custom BT node
        # system's own Python nodes -- persisted per-file .py files under
        # custom_behaviors/custom_node_files/ from a previous sync.
        self.xparo_engine.sync_custom_node_files()
        # Before connect(), so the first connection asks for the ad schedule.
        self.xparo_engine.setup_ads(self.xparo_ads_display, node=self)
        self.xparo_engine.connect()
        self.create_timer(SELF_DIAGNOSTICS_PERIOD_SEC, self._guarded(self._publish_self_diagnostics))
        self._other_xparo_checked = False
        self._other_xparo_timer = self.create_timer(OTHER_XPARO_CHECK_SEC, self._guarded(self._warn_if_other_xparo))
        self.create_timer(HEALTH_FLUSH_SEC, self._guarded(self.xparo_engine.flush_health))

    def _warn_if_other_xparo(self):
        """Once, a few seconds after start: another robot's xparo on the same
        ROS domain means shared /joy and task topics (round 2, V1)."""
        if self._other_xparo_checked:
            return
        self._other_xparo_checked = True
        try:
            self._other_xparo_timer.cancel()
        except Exception:
            pass
        others = other_xparo_nodes(self.get_node_names_and_namespaces(), self.get_namespace())
        if others:
            self.get_logger().error(
                f"Another xparo node is on this ROS network ({len(others)} more): robots on one ROS_DOMAIN_ID share "
                f"/joy and /xparo/run_task, so commands for one reach all. Give each robot its own ROS_DOMAIN_ID.")

    def _publish_self_diagnostics(self):
        """xparo's own health on the standard /diagnostics topic (1 Hz, the
        usual diagnostic_updater rate): dashboard connection, rosbag
        recorder, task engine, disk. rqt_robot_monitor and friends see it,
        and this node's own DiagnosticsAggregator picks it up like any
        other publisher's."""
        try:
            statuses = self.xparo_engine.self_health_statuses()
        except Exception as e:
            self.get_logger().warn(f"couldn't build xparo's own diagnostics: {e}")
            return
        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        hardware_id = self.xparo_engine.local_database.unique_id
        for name, level, message in statuses:
            status = DiagnosticStatus()
            status.level = _DIAG_LEVELS.get(level, DiagnosticStatus.ERROR)
            status.name = name
            status.message = message
            status.hardware_id = hardware_id
            msg.status.append(status)
        self.self_diagnostics_pub.publish(msg)

    def _request_restart(self):
        """Called from the OTA update thread once the new version is built:
        stops spinning; main() then shuts down cleanly and re-executes this
        same process with the new code."""
        self.get_logger().info("Restarting xparo to load the updated version...")
        self.restart_requested = True
        rclpy.try_shutdown()

    def _on_task_event(self, kind, info):
        """Engine.on_task_event: a task's start and result, in this node's
        log and (results) on /xparo/task_result -- so a task triggered with
        `ros2 topic pub /xparo/run_task ...` visibly does something here."""
        title = info.get("task_title") or info.get("task_id") or "task"
        run = f"run {info.get('run_id')}" if info.get("run_id") else "not started"
        if kind == "started":
            limit = f", time limit {info['timeout_s']:.0f}s" if info.get("timeout_s") else ""
            self.get_logger().info(f"Task '{title}' started ({run}, trigger {info.get('trigger') or '-'}{limit})")
            return
        if info.get("success"):
            self.get_logger().info(f"Task '{title}' finished: SUCCESS in {float(info.get('duration_s') or 0):.1f}s ({run})")
        else:
            why = info.get("explanation") or info.get("error") or info.get("outcome") or "failed"
            if info.get("error") and info["error"] not in why:
                why = f"{why} {info['error']}"  # e.g. which node is invalid, not just "it has errors"
            self.get_logger().warn(f"Task '{title}' FAILED ({info.get('outcome') or 'failed'}, {run}): {why}")
        out = String()
        out.data = json.dumps(info, default=str)
        self.task_result_pub.publish(out)




    def navigation_callback(self, msg):
        try:
            for event in msg.event_log:
                ts = event.timestamp.sec + event.timestamp.nanosec * 1e-9
                dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
                data = {
                    "node_name": event.node_name,
                    "node_type": event.node_name,
                    "uid": event.uid,
                    "prev": str(event.previous_status),
                    "curr": str(event.current_status),
                    "timestamp": ts,
                    "datetime": dt
                }
                self.xparo_engine.add_live_update(data)

        except Exception as e:
            self.get_logger().error(f"Error processing message: {e}")

    def _load_tethered_channels_config(self, path):
        """Returns a list of {"name","host","port"} dicts (transports
        .tethered_tcp.TetheredTcpTransport's channels_config shape), or
        None (that class's own DEFAULT_CHANNELS_CONFIG -- loopback test
        values, not a real link) if path is empty or unreadable. Never
        raises -- a bad path here shouldn't take the whole node down,
        just fall back the same as not setting it at all.
        """
        if not path:
            return None
        try:
            with open(path, 'r') as f:
                data = yaml.safe_load(f) or {}
            channels = data.get("channels", [])
            return [{"name": c["name"], "host": c["host"], "port": int(c["port"])} for c in channels] or None
        except Exception as e:
            self.get_logger().error(f"Failed to load tethered_channels_config_path={path!r}: {e}")
            return None

    def _publish_joy(self, axes, buttons):
        """Passed into Engine as joy_publish -- remote_ops.handle_teleop
        calls this with already-padded axes/buttons (see MIN_JOY_AXES/
        MIN_JOY_BUTTONS in remote_ops.py)."""
        msg = Joy()
        msg.axes = axes
        msg.buttons = buttons
        self.joy_pub.publish(msg)

    ## the data can be used a voice speak
    def call_message(self,message,**kwargs):
        try:
            vv = String()
            vv.data = json.dumps(message)
            self.dash_send.publish(vv)
        except Exception as e:
            print(e)

    def live_updates(self,msg):
        try:
            self.xparo_engine.add_live_update(json.loads(msg.data))
        except Exception as e:
            self.get_logger().error(f'Failed to process live history: {str(e)}')

    def task_updates(self,msg):
        try:
            self.xparo_engine.add_task_history(json.loads(msg.data))
        except Exception as e:
            self.get_logger().error(f'Failed to process task history: {str(e)}')

    def _offline_reply(self, topic):
        """Asking while offline used to fail silently, leaving a voice app
        waiting on /xparo/response forever. Answer right away instead."""
        if self.xparo_engine.is_online():
            return False
        self.call_message({"message": {"error": "offline", "responded_by": "robot",
                                       "bot_response": "I'm offline right now, so I can't ask the AI. Try again when I'm connected."}})
        self.get_logger().warn(f"{topic}: not connected to XPARO, question not sent")
        return True

    def ask_question_fun(self, msg):
        if self._offline_reply('/xparo/ask'):
            return
        self.xparo_engine.send(str(msg.data))

    def ask_task_cb(self, msg):
        if self._offline_reply('/xparo/ask_task'):
            return
        self.xparo_engine.ask_task_call(msg.data)

    def task_call_confirm_cb(self, msg):
        action = str(msg.data).strip().lower()
        offer = self.xparo_engine.task_call
        handled = offer.confirm() if action == "call" else offer.cancel() if action == "cancel" else None
        if handled is None:
            self.get_logger().error('/xparo/task_call_confirm expects "call" or "cancel"')
        elif not handled:
            self.get_logger().warn("No AI task call is waiting for an answer")

    def _publish_task_call(self, info):
        out = String()
        out.data = json.dumps(info)
        self.task_call_pub.publish(out)
        self.get_logger().info(f"AI task call '{info.get('title') or info.get('task_id')}': {info.get('status')}")

    def _guarded(self, callback):
        """Every topic callback goes through this: one bad message (wrong
        JSON shape, a bug) is logged instead of escaping rclpy.spin() and
        killing the whole node."""
        def wrapper(*args):
            try:
                return callback(*args)
            except Exception as e:
                msg = args[0] if args else None
                self.get_logger().error(f"{getattr(callback, '__name__', 'callback')} failed on {str(getattr(msg, 'data', msg))[:200]!r}: {e}")
        return wrapper

    def run_task_topic_cb(self, msg):
        try:
            payload = json.loads(msg.data)
        except Exception as e:
            self.get_logger().error(f'Failed to parse /xparo/run_task message: {str(e)}')
            return
        if isinstance(payload, str):
            payload = {"task_id": payload}  # a bare task id is fine too
        if not isinstance(payload, dict):
            self.get_logger().error('/xparo/run_task expects {"task_id": "...", "override_params": {...}}')
            return
        task_id = payload.get("task_id")
        if not task_id or not isinstance(task_id, (str, int)):
            self.get_logger().error('/xparo/run_task message missing "task_id"')
            return
        params = payload.get("override_params")
        if params is not None and not isinstance(params, dict):
            self.get_logger().error('/xparo/run_task "override_params" must be an object like {"speed": "1.0"}')
            return
        self.xparo_engine.run_task_from_topic(str(task_id), params)

    def cancel_task_cb(self, msg):
        raw = str(msg.data).strip()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        if isinstance(payload, dict):
            stopped = run_task.cancel_task(task_id=payload.get("task_id"), run_id=payload.get("run_id"),
                                           reason="cancelled on the robot (/xparo/cancel_task)")
        else:
            stopped = run_task.cancel_task(task_id=str(payload), reason="cancelled on the robot (/xparo/cancel_task)")
        if stopped:
            self.get_logger().info(f"Stopping {len(stopped)} run(s): {', '.join(stopped)}")
        else:
            self.get_logger().warn(f"/xparo/cancel_task: no running task matches {raw!r}")
    ####################################################
    ####################################################

        





def clean_setting(value):
    """'  "abc-123"\n' -> 'abc-123'."""
    return str(value or "").strip().strip('"\'').strip()


def check_connection_settings(project_id, secret_key):
    """Plain-language problems with the configured ids (empty list = fine)."""
    import re
    problems = []
    if not project_id:
        problems.append("xparo_project_id is empty: copy the project ID from the dashboard (Keys & credits).")
    elif not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", project_id):
        problems.append(f"xparo_project_id {project_id!r} doesn't look like a project ID "
                        "(it should look like 1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed).")
    if not secret_key:
        problems.append("xparo_secret_key is empty: create a secret key on the dashboard (Keys & credits).")
    elif any(c.isspace() for c in secret_key) or "/" in secret_key:
        problems.append("xparo_secret_key contains spaces or '/': paste only the key itself.")
    return problems


OTHER_XPARO_CHECK_SEC = 10.0


def other_xparo_nodes(node_names_and_namespaces, own_namespace="/"):
    """Other xparo nodes visible on this ROS graph (another robot on the
    same ROS_DOMAIN_ID): [(name, namespace), ...] minus this node."""
    seen = [(n, ns) for n, ns in node_names_and_namespaces if n == "xparo"]
    others = list(seen)
    if (("xparo", own_namespace)) in others:
        others.remove(("xparo", own_namespace))
    return others


def check_run_environment(package_dir):
    """Warnings for the classic 'ran it with sudo once' mess: files the
    node must rewrite end up owned by root and every later normal run fails
    to save credentials, tasks and trees."""
    problems = []
    try:
        if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
            problems.append("xparo is running with sudo. Files it writes become owned by root and later runs "
                            "without sudo can't update them. Run it as your normal user.")
    except AttributeError:
        return problems
    if os.environ.get("ROS_DOMAIN_ID", "0").strip() in ("", "0"):
        problems.append("ROS_DOMAIN_ID is not set (0). Every robot on this network shares /joy, /xparo/run_task "
                        "and /xparo/cancel_task: driving or tasking one robot reaches all of them. Give each robot "
                        "its own number, e.g. `export ROS_DOMAIN_ID=12` in ~/.bashrc (1-101).")
    for sub in ("config", "custom_behaviors"):
        path = os.path.join(package_dir, sub)
        if os.path.isdir(path) and not os.access(path, os.W_OK):
            problems.append(f"{path} isn't writable by this user (probably created by a sudo run). "
                            f"Fix it with: sudo chown -R $USER {path}")
    return problems


def acquire_single_instance_lock(path):
    """An exclusive, non-blocking lock held for the life of the process
    (released automatically if it dies). None if another process has it."""
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def wait_for_instance_lock(path, retry_s=30, sleep=time.sleep):
    while True:
        handle = acquire_single_instance_lock(path)
        if handle is not None:
            return handle
        sleep(retry_s)


def main(args=None):
    rclpy.init(args=args)
    xparo = Xparo()
    try:
        rclpy.spin(xparo)
    except (KeyboardInterrupt, ExternalShutdownException):
        # SIGINT (Ctrl+C, ros2 launch's first stop signal) or SIGTERM /
        # another shutdown -- either way, finish below and exit.
        pass
    # Zero the sticks before anything else: a base that keeps its last
    # cmd_vel would otherwise keep driving after Ctrl+C, a launch stop or
    # an update restart (the deadman thread dies with the process).
    try:
        xparo.xparo_engine.teleop.stop()
    except Exception as e:
        xparo.get_logger().warn(f"Couldn't send the final teleop stop: {e}")
    # Sends this session's final Logs_history update (total runtime,
    # average CPU/RAM/disk/GPU consumption across the whole session) while
    # the transport can still actually send it -- only reachable from a
    # graceful stop (Ctrl+C / `ros2 launch` shutting the process down),
    # never a genuine force-kill/crash, the same honest limitation
    # blackbox_manager.py's own rosbag force-exit handling already has.
    try:
        xparo.xparo_engine.local_database.stop_logging_session(xparo.xparo_engine.private_send)
    except Exception as e:
        xparo.get_logger().error(f"Failed to send final logging session update: {e}")
    if xparo.xparo_engine.ad_manager is not None:
        xparo.xparo_engine.ad_manager.stop()
    try:
        # A clean websocket close, so the dashboard shows the robot
        # offline right away instead of after the heartbeat timeout.
        xparo.xparo_engine.transport.close()
    except Exception as e:
        xparo.get_logger().warn(f"Couldn't close the dashboard connection cleanly: {e}")
    try:
        xparo.destroy_node()
    except Exception:
        pass  # context may already be shut down (restart path)
    rclpy.try_shutdown()
    if xparo.restart_requested:
        # Same interpreter, same command line (orig_argv keeps a `-m
        # xparo.xparo_ros` if that's how it was started) and same PID, so
        # ros2 launch / systemd / the terminal keep owning it. Python and
        # everything it loaded is replaced by the freshly built version.
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + list(sys.orig_argv[1:]))


if __name__ == '__main__':
    main()
