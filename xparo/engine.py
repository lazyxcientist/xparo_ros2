import json
import threading
import time
import os
from datetime import datetime
from .database import XP_Database
from .transports.django_ws import DjangoWsTransport
from . import remote_ops

record_bags = False
xparo_database_size =  80

# Pairs with apps.analytics.models.ROBOT_ONLINE_THRESHOLD_SECONDS (90s) on
# the Django side -- sending every 30s tolerates one missed beat before a
# robot flips to "offline" there.
HEARTBEAT_INTERVAL_SECONDS = 30

# Status mapping
STATUS_MAP = {
    "IDLE": 0,
    "RUNNING": 1,
    "SUCCESS": 2,
    "FAILURE": 3,
    # Add more if needed
}


class Engine():
    def __init__(self,secret_key,project_id,connection_type = "websocket",record_bags=record_bags,BAG_DIR=None,environment=None,rosbag_control=None,joy_publish=None,xparo_transport="django_ws",tethered_channels_config=None):
        global xparo_database_size
        self.tmp_folder = "."
        xparo_database_path = os.path.join(self.tmp_folder,"xparo",project_id,'database')
        # BAG_DIR must arrive through the constructor, same reasoning as
        # record_bags just below -- XP_Database (and, if recording, the
        # BlackboxOrchestrator it starts) is built before __init__ returns,
        # so a caller setting self.BAG_DIR afterward (as xparo_ros.py used
        # to) has no effect on where bags actually get written.
        self.BAG_DIR = BAG_DIR or os.path.join(self.tmp_folder,"xparo",project_id,'ros_bags')
        self.xparo_folder = os.path.abspath(os.path.join( os.path.dirname(__file__), os.pardir))
        self.connection_type = connection_type #"websocket" # "rest" , "websocket" , "hybrid" , "offline"
        # joy_publish(axes, buttons) -> None -- publishing a real
        # sensor_msgs/Joy message needs an actual ROS2 node context, which
        # Engine deliberately doesn't depend on (it's usable standalone --
        # see the __main__ block at the bottom of this file). xparo_ros.py
        # supplies the real one; defaults to a no-op so TELEOP is a safe
        # no-op (still acks) rather than a crash outside a live node.
        self.joy_publish = joy_publish or (lambda axes, buttons: None)
        # Base dir for LIST_FILES/DELETE_FILE/FILE_REQ -- deliberately
        # separate from BAG_DIR (rosbag sessions) and the xparo_* config
        # paths below (behavior trees/env/properties, a different concept).
        self.transfer_dir = os.path.join(self.xparo_folder, 'transferred_files')
        self.file_transfer = remote_ops.FileTransferSession(self.transfer_dir)

        self.xparo_behavior_path = os.path.join(self.xparo_folder,'config','default.xml')
        self.xparo_file_path = os.path.join(self.xparo_folder,'config','default.txt')
        self.xparo_env_path = os.path.join(self.xparo_folder,'config','default.env')
        self.xparo_local_env_path = os.path.join(self.xparo_folder,'config','local.env')
        self.xparo_properties_path = os.path.join(self.xparo_folder,'config',"properties.txt")
        self.xparo_custom_behaviors_folder_path = os.path.join(self.xparo_folder,'custom_behaviors')
        self.xparo_custom_files_folder_path = os.path.join(self.xparo_folder,'custom_files')
        self.xparo_custom_evns_folder_path = os.path.join(self.xparo_folder,'custom_envs')
        # Per-robot credential (apps/analytics/models.py's RobotCredential),
        # issued once by ADD_robots_info the first time this device_id is
        # ever seen and persisted here so every reconnect after that uses
        # it instead of the project-wide secret_key constructor arg -- see
        # the ROBOT_CREDENTIAL branch in on_ws_message below for where it's
        # written, and the loader right after this dict for where it's read
        # back on startup.
        self.xparo_credential_path = os.path.join(self.xparo_folder,'config','credential.json')
        self.record_bags = record_bags
        self.files = {'behavior'         : self.xparo_behavior_path,
                    'file'         : self.xparo_file_path,
                    'env'         : self.xparo_env_path,
                    'local_env'         : self.xparo_local_env_path,
                    'properties'   : self.xparo_properties_path,
                    'xparo_custom_behaviors_folder_path'   : self.xparo_custom_behaviors_folder_path,
                    'xparo_custom_files_folder_path'   : self.xparo_custom_files_folder_path,
                    'xparo_custom_evns_folder_path'   : self.xparo_custom_evns_folder_path,
                        }

        effective_secret = self._load_persisted_credential() or secret_key

        # Everything about *how* a message actually gets to its peer lives
        # in the transport (see transports/base.py's Transport ABC
        # docstring) -- Engine only knows how to build/interpret messages,
        # and drives the transport through on_message (dispatch table
        # below) and on_connected (initial handshake). xparo_transport
        # picks which one: "django_ws" (networked robots, the only option
        # that existed before Phase 4) or "tethered_tcp" (a physically-
        # tethered ROV with no path to Django at all -- see
        # transports/tethered_tcp.py's module docstring). Both call the
        # exact same on_ws_message dispatch table below.
        if xparo_transport == "tethered_tcp":
            from .transports.tethered_tcp import TetheredTcpTransport
            self.transport = TetheredTcpTransport(
                on_message=self.on_ws_message,
                on_connected=self.send_initial_data,
                channels_config=tethered_channels_config,
            )
        else:
            self.transport = DjangoWsTransport(
                effective_secret, project_id,
                on_message=self.on_ws_message,
                on_connected=self.send_initial_data,
                connection_type=connection_type,
                environment=environment,
            )

        # tethered_tcp has no Django to talk to at all (that's the whole
        # reason it exists) -- website_base_url only exists on
        # DjangoWsTransport. record_bags still records locally either way
        # (RosbagControl doesn't touch Django); the cloud-upload half of
        # that feature (BlackboxOrchestrator._process_uploads) simply has
        # nowhere to POST to under tethered_tcp and safely no-ops (its own
        # try/except already treats a failed upload as "retry next cycle",
        # not a crash).
        xparo_website_url = getattr(self.transport, 'website_base_url', None)
        self.local_database = XP_Database(xparo_database_size,
                                            xparo_database_path,
                                            xparo_website_url,
                                            self.BAG_DIR,self.record_bags,rosbag_control)
        try:
            threading.Thread(target=self._logging_update_loop, daemon=True).start()
        except:
            print("you are offline")

    def _load_persisted_credential(self):
        """Returns the raw credential value from a prior ROBOT_CREDENTIAL
        response, or None if this device has never been issued one yet
        (brand new robot, or a pre-Phase-1 deployment that hasn't
        reconnected since). Never raises -- a missing/corrupt file just
        means "fall back to the constructor's secret_key", same as before
        this existed.
        """
        try:
            with open(self.xparo_credential_path, 'r') as file:
                return json.load(file).get('value') or None
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _persist_credential(self, raw_value):
        os.makedirs(os.path.dirname(self.xparo_credential_path), exist_ok=True)
        with open(self.xparo_credential_path, 'w') as file:
            json.dump({'value': raw_value}, file)

    def _logging_update_loop(self):
        # Heartbeat runs unconditionally, every HEARTBEAT_INTERVAL_SECONDS,
        # independent of _stop_updates/session_id -- Robots.is_online must
        # not depend on a rosbag/logging session being active. The original
        # resource/log-session update keeps its own cadence and gating,
        # folded into the same loop rather than a second thread.
        seconds_since_resource_update = 0
        while True:
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)

            self.private_send(json.dumps({"ROBOT_HEARTBEAT": {"device_id": self.local_database.unique_id}}),
                               command_for="rest")

            seconds_since_resource_update += HEARTBEAT_INTERVAL_SECONDS
            if seconds_since_resource_update >= self.local_database.update_interval:
                seconds_since_resource_update = 0
                if not self.local_database._stop_updates and self.local_database.session_id:
                    self.local_database.update_logging_session(self.private_send)

    #########################################################################################
    def connect(self):
        self.transport.connect()

    def private_send(self,message,command_for=None):
        return self.transport.send(message, command_for=command_for)

    def _send_dict(self, payload):
        """remote_ops.py's handlers take a send_response(dict) callback
        (transport-agnostic -- see its module docstring); private_send
        wants a JSON string. This is the adapter between the two.
        """
        self.private_send(json.dumps(payload))

    def send(self,message,remote_name="default"):
        filtered_data = json.dumps({"ask_bot_api":{"unique_id":self.local_database.unique_id, "data":{"from robot":"testing one.."},"question":message}})
        self.private_send(filtered_data,
                        #   command_for="rest"
                          )

    def add_task_history(self,message):
        filtered_data = json.dumps({"ADD_Task_history_database":{"unique_id":self.local_database.unique_id,
                                                                "input_data": message.get("input_data", {}),
                                                                "output_data": message.get("output_data", {}),
                                                                "type": message.get("type", "generic_task"),
                                                                "created_at": message.get("created_at", datetime.now().isoformat())
                                                                }})
        self.private_send(filtered_data,
                        #   command_for="rest"
                          )

    def add_live_update(self,message):
        filtered_data = json.dumps({"ADD_live_update_bt":{"unique_id":self.local_database.unique_id,

                                                                "node_name": message.get("node_name", ""),
                                                                "node_type": message.get("node_type", ""),
                                                                "uid": message.get("uid", 0),
                                                                "prev": message.get("prev", ""),
                                                                "curr": message.get("curr", ""),
                                                                "timestamp": message.get("timestamp", ""),
                                                                "datetime": message.get("datetime", ""),

                                                                }})
        self.private_send(filtered_data,
                        #   command_for="rest"
                          )



    def live_updates(self, msg):
        try:
            data = json.loads(msg.data)

            # Extract fields with defaults
            node_name = data.get("node_name", "")
            node_type = data.get("node_type", "")
            uid = data.get("uid", "")
            prev = data.get("prev", "")
            curr = data.get("curr", "")
            timestamp = data.get("timestamp")
            datetime_str = data.get("datetime", "")

            # Generate timestamp if missing
            if timestamp is None:
                timestamp = time.time()
            elif isinstance(timestamp, str):
                # Attempt to parse string timestamp? Not likely; assume numeric.
                timestamp = float(timestamp)

            # Generate datetime if missing
            if not datetime_str:
                dt = datetime.fromtimestamp(timestamp)
                datetime_str = dt.strftime("%Y-%m-%d %H:%M:%S")


            filtered_data = json.dumps({"ADD_live_update_database":{
                                                                    "unique_id":self.local_database.unique_id,
                                                                    "node_name": node_name,
                                                                    "node_type": node_type,
                                                                    "uid": uid,
                                                                    "prev": STATUS_MAP.get(prev, -1),
                                                                    "curr": STATUS_MAP.get(curr, -1),
                                                                    "timestamp": timestamp,
                                                                    "datetime": datetime_str,
                                                                    }})
            self.private_send(filtered_data,
                              command_for="websocket"
                            )
        except Exception as e:
            print(f'Failed to process live history: {str(e)}')

    def on_ws_message(self, ws, message):
        print(message)
        print("json recived...")
        if type(message)!=dict:
            message = json.loads(message)
        for k,val in message.items():
            if k=="title":
                pass
            elif k=="disc":
                pass
            elif k=="goal":
                pass
            elif k=="rules":
                pass
            elif k=="aiml":
                content =  f'''<root BTCPP_format="4" main_tree_to_execute="MainTree">
<BehaviorTree ID="MainTree">
{val}
</BehaviorTree>
</root>'''
                self.local_database.load_or_create_file(self.files["behavior"],content)
                with open(self.files["behavior"], 'w') as file:
                    file.write(content)
            elif k=="maps":
                content =  f'''{val}'''
                self.local_database.load_or_create_file(self.files["env"],content)
                with open(self.files["env"], 'w') as file:
                    file.write(content)
            elif k=="local_env":
                content =  f'''{val}'''
                self.local_database.load_or_create_file(self.files["local_env"],content)
                with open(self.files["local_env"], 'w') as file:
                    file.write(content)
            elif k=="Sets":
                content =  f'''{val}'''
                self.local_database.load_or_create_file(self.files["file"],content)
                with open(self.files["file"], 'w') as file:
                    file.write(content)
            elif k=="properties":
                content =  f'''{val}'''
                self.local_database.load_or_create_file(self.files["properties"],content)
                with open(self.files["properties"], 'w') as file:
                    file.write(content)
            elif k=="custom_aiml":
                for kk,vv in val.items():
                    pth = os.path.join(self.files["xparo_custom_behaviors_folder_path"],kk+'.xml')
                    content =  f'''<root BTCPP_format="4" main_tree_to_execute="MainTree">
<BehaviorTree ID="MainTree">
{vv}
</BehaviorTree>
</root>'''
                    self.local_database.load_or_create_file(pth,content)
                    with open(pth, 'w') as file:
                        file.write(content)
            elif k=="custom_maps":
                for kk,vv in val.items():
                    pth = os.path.join(self.files["xparo_custom_evns_folder_path"],kk+'.env')
                    content =  f'''{vv}'''
                    self.local_database.load_or_create_file(pth,content)
                    with open(pth, 'w') as file:
                        file.write(content)
            elif k=="custom_Sets" or k=="custom_sets":
                for kk,vv in val.items():
                    pth = os.path.join(self.files["xparo_custom_files_folder_path"],kk)
                    content =  f'''{vv}'''
                    self.local_database.load_or_create_file(pth,content)
                    with open(pth, 'w') as file:
                        file.write(content)
            elif k=="ROBOT_CREDENTIAL":
                self._persist_credential(val)
            elif k=="get_initial_local_env_data":
                self.get_initial_local_env_data()
            elif k=="sync_local_database":
                self.get_local_files()
            elif k=="log_updated":
                self.local_database.dashboard_receive({"log_updated":val},self.private_send)
                self.local_database._stop_updates = False
            elif k=="REST_API_TOKEN":
                # dashboard_receive already has a correct handler for this
                # (arms orchestrator.API_TOKEN and flushes any queued
                # uploads) -- it was just never reachable from here.
                self.local_database.dashboard_receive({"REST_API_TOKEN":val},self.private_send)
            # ---- Phase 4 remote-ops -- see remote_ops.py's module
            # docstring for why these are plain function calls here rather
            # than inline logic: the exact same handlers drive both this
            # transport and transports/tethered_tcp.py.
            elif k=="RUN_COMMAND":
                command = val.get("command", "")
                request_id = val.get("request_id")
                timeout = remote_ops.clamp_command_timeout(val.get("timeout"))
                if command.strip():
                    threading.Thread(
                        target=remote_ops.handle_run_command,
                        args=(command, request_id, timeout, self._send_dict),
                        daemon=True,
                    ).start()
                else:
                    self._send_dict({"COMMAND_RESULT": {
                        "request_id": request_id, "command": command,
                        "success": False, "exit_code": None, "timed_out": False,
                        "output": "(empty command)", "truncated": False,
                    }})
            elif k=="TELEOP":
                remote_ops.handle_teleop(val.get("axes", []), val.get("buttons", []), self.joy_publish, self._send_dict)
            elif k=="LIST_FILES":
                remote_ops.handle_list_files(self.transfer_dir, self._send_dict)
            elif k=="DELETE_FILE":
                remote_ops.handle_delete_file(self.transfer_dir, val.get("path", ""), self._send_dict)
            elif k=="FILE_REQ":
                self.file_transfer.handle_file_req(val, self._send_dict)
            elif k=="FILE_CHUNK":
                self.file_transfer.handle_file_chunk(val)
            elif k=="FILE_COMPLETE":
                self.file_transfer.handle_file_complete(self._send_dict)
            else:
                self.call_message(message)


    def get_initial_local_env_data(self):
        try:
            with open(self.files["local_env"], 'r') as file:
                content = file.read()
                filtered_data = json.dumps({"ADD_robots_maps":{"device_id":self.local_database.unique_id, "maps":content}})
                self.private_send(filtered_data,
                                #   command_for="rest"
                                )
        except Exception as e:
            print(e)
            return ""

    def get_local_files(self):
        """
        Reads all local configuration files (aiml, maps, Sets, properties,
        custom_aiml, custom_maps, custom_sets) and returns them in the structure
        expected by the server.
        """
        result = {
            "aiml": "",
            "maps": "",
            "Sets": "",
            "properties": "",
            "custom_aiml": {},
            "custom_maps": {},
            "custom_sets": {}
        }

        # ---- Read main aiml (behavior tree) ----
        behavior_path = self.files["behavior"]
        if os.path.exists(behavior_path):
            with open(behavior_path, 'r') as f:
                content = f.read()
                # Extract the content between <BehaviorTree ID="MainTree"> and </BehaviorTree>
                start = content.find('<BehaviorTree ID="MainTree">')
                if start != -1:
                    start += len('<BehaviorTree ID="MainTree">')
                    end = content.find('</BehaviorTree>', start)
                    if end != -1:
                        result["aiml"] = content[start:end].strip()
                else:
                    # Fallback: send the whole file if extraction fails
                    result["aiml"] = content

        # ---- Read maps (env file) ----
        env_path = self.files["env"]
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                result["maps"] = f.read()

        # ---- Read Sets (file) ----
        sets_path = self.files["file"]
        if os.path.exists(sets_path):
            with open(sets_path, 'r') as f:
                result["Sets"] = f.read()

        # ---- Read properties ----
        properties_path = self.files["properties"]
        if os.path.exists(properties_path):
            with open(properties_path, 'r') as f:
                result["properties"] = f.read()

        # ---- Read custom_aiml (all .xml files in custom_behaviors folder) ----
        custom_behaviors_path = self.files["xparo_custom_behaviors_folder_path"]
        if os.path.exists(custom_behaviors_path):
            for filename in os.listdir(custom_behaviors_path):
                if filename.endswith('.xml'):
                    filepath = os.path.join(custom_behaviors_path, filename)
                    with open(filepath, 'r') as f:
                        content = f.read()
                        # Extract inner content like for main aiml
                        start = content.find('<BehaviorTree ID="MainTree">')
                        if start != -1:
                            start += len('<BehaviorTree ID="MainTree">')
                            end = content.find('</BehaviorTree>', start)
                            if end != -1:
                                result["custom_aiml"][filename[:-4]] = content[start:end].strip()
                            else:
                                result["custom_aiml"][filename[:-4]] = content
                        else:
                            result["custom_aiml"][filename[:-4]] = content

        # ---- Read custom_maps (all .env files in custom_envs folder) ----
        custom_envs_path = self.files["xparo_custom_evns_folder_path"]
        if os.path.exists(custom_envs_path):
            for filename in os.listdir(custom_envs_path):
                if filename.endswith('.env'):
                    filepath = os.path.join(custom_envs_path, filename)
                    with open(filepath, 'r') as f:
                        result["custom_maps"][filename[:-4]] = f.read()

        # ---- Read custom_sets (all files in custom_files folder) ----
        custom_files_path = self.files["xparo_custom_files_folder_path"]
        if os.path.exists(custom_files_path):
            for filename in os.listdir(custom_files_path):
                filepath = os.path.join(custom_files_path, filename)
                if os.path.isfile(filepath):
                    with open(filepath, 'r') as f:
                        result["custom_sets"][filename] = f.read()

        try:
            with open(self.files["local_env"], 'r') as file:
                content = file.read()
                filtered_data = json.dumps({"save_aiml":result})
                self.private_send(filtered_data,
                                #   command_for="rest"
                                )
        except Exception as e:
            print(e)
            return ""

    def send_initial_data(self):
        # self.private_send(json.dumps({"initisilaze_api":{}}))
        self.local_database.dashboard_receive({"needed_robot_data":{"sent":True}},self.private_send)

    ##################################
    ###### function override #########
    ##################################

    def call_message(self,message,**kwargs):
        print(f"fun waiting to overrite :- {message}")


    #################################################################################


if __name__ == "__main__":
    ai_brain = Engine("secret_key","project_id")
