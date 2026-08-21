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
from rclpy.node import Node
from std_msgs.msg import String
from sensor_msgs.msg import Joy
from ament_index_python.packages import get_package_share_directory
import json
import os
import yaml
from datetime import datetime


from xparo.engine import Engine
from xparo.rosbag_control import RosbagControl, load_rosbag_config
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
        self.xparo_project_id = self.declare_parameter("xparo_project_id", xparo_config.get("xparo_project_id", "")  ).value
        self.xparo_secret_key = self.declare_parameter("xparo_secret_key", xparo_config.get("xparo_secret_key", "") ).value
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


        self.ask_question = self.create_subscription(String, '/xparo/ask', self.ask_question_fun, 10)
        self.task_updates = self.create_subscription(String,'/xparo/task_updates',self.task_updates,10)
        self.bt_log = self.create_subscription(String,'/bt_xparo_log',self.live_updates,10)
        # Behaviour Tree redesign: local task execution -- publish
        # {"task_id": "...", "override_params": {...}} here (override_params
        # optional) to run an already-synced task (Engine.sync_custom_tasks)
        # directly on this robot, no Django round trip needed at trigger
        # time. Same naming convention as /xparo/ask, /xparo/task_updates.
        self.run_task_sub = self.create_subscription(String, '/xparo/run_task', self.run_task_topic_cb, 10)
        self.dash_send = self.create_publisher(String,"/xparo/response",10)
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
        # own __main__ block). Only construct it when actually recording.
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
        ) if self.record_bags else None

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
        self.xparo_engine.connect()




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

    def ask_question_fun(self, msg):
        user_input = str(msg.data)
        self.xparo_engine.send(user_input.upper())

    def run_task_topic_cb(self, msg):
        try:
            payload = json.loads(msg.data)
        except Exception as e:
            self.get_logger().error(f'Failed to parse /xparo/run_task message: {str(e)}')
            return
        task_id = payload.get("task_id")
        if not task_id:
            self.get_logger().error('/xparo/run_task message missing "task_id"')
            return
        self.xparo_engine.run_task_from_topic(task_id, payload.get("override_params"))
    ####################################################
    ####################################################

        





def main(args=None):
    rclpy.init(args=args)
    xparo = Xparo()
    try:
        rclpy.spin(xparo)
    except KeyboardInterrupt:
        pass
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
    xparo.destroy_node()
    rclpy.try_shutdown()


if __name__ == '__main__':
    main()
