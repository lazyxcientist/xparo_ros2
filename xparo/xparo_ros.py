#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from ament_index_python.packages import get_package_share_directory
import json
import os
from datetime import datetime
try:
    from .xparo import Engine
except:
    from xparo.xparo import Engine

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
        self.xparo_project_id = self.declare_parameter("xparo_project_id", xparo_config.get("xparo_project_id", "d6f86221-67c4-4068-9ac4-05c95e9b5ca9")  ).value
        self.xparo_secret_key = self.declare_parameter("xparo_secret_key", xparo_config.get("xparo_secret_key", "135b4434b014355ee4bfab455bb6d81a09463a169b6b6bc70907f0946c9a995c") ).value
        self.xparo_connection_type = self.declare_parameter("xparo_connection_type", xparo_config.get("xparo_connection_type", "websocket")  ).value
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
        

        self.ask_question = self.create_subscription(String, '/xparo/ask', self.ask_question_fun, 10)
        self.task_updates = self.create_subscription(String,'/xparo/task_updates',self.task_updates,10)
        self.bt_log = self.create_subscription(String,'/bt_xparo_log',self.live_updates,10)
        self.dash_send = self.create_publisher(String,"/xparo/response",10)


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
        
        self.xparo_engine = Engine(self.xparo_secret_key,
                              self.xparo_project_id,
                              connection_type = self.xparo_connection_type)
        self.xparo_engine.call_message=self.call_message
        self.xparo_engine.files = self.files
        self.xparo_engine.BAG_DIR = self.BAG_DIR
        self.xparo_engine.record_bags = self.record_bags
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
    ####################################################
    ####################################################

        





def main(args=None):
    rclpy.init(args=args)
    xparo = Xparo()
    try:
        rclpy.spin(xparo)
    except KeyboardInterrupt:
        pass
    xparo.destroy_node()
    rclpy.try_shutdown()


if __name__ == '__main__':
    main()
