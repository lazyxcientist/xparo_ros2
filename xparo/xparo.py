import websocket
import json
import threading
import requests
import time
import os
from datetime import datetime
try:
    from .database import XP_Database
except:
    from xparo.database import XP_Database

local = False
record_bags = False
xparo_database_size =  80

# Status mapping
STATUS_MAP = {
    "IDLE": 0,
    "RUNNING": 1,
    "SUCCESS": 2,
    "FAILURE": 3,
    # Add more if needed
}



############### websocket #############
#######################################
class Xparo_socket(websocket.WebSocketApp):
    def __init__(self, *args, **kwargs):
        super(Xparo_socket, self).__init__(*args, **kwargs)





class Engine():
    def __init__(self,secret_key,project_id,connection_type = "websocket"):
        global xparo_database_size
        self.websocket_connected = False
        self.tmp_folder = "."
        xparo_database_path = os.path.join(self.tmp_folder,"xparo",project_id,'database')
        self.BAG_DIR = os.path.join(self.tmp_folder,"xparo",project_id,'ros_bags')
        self.xparo_folder = os.path.abspath(os.path.join( os.path.dirname(__file__), os.pardir))
        self.connection_type = connection_type #"websocket" # "rest" , "websocket" , "hybrid" , "offline"

        self.xparo_behavior_path = os.path.join(self.xparo_folder,'config','default.xml')
        self.xparo_file_path = os.path.join(self.xparo_folder,'config','default.txt')
        self.xparo_env_path = os.path.join(self.xparo_folder,'config','default.env')
        self.xparo_local_env_path = os.path.join(self.xparo_folder,'config','local.env')
        self.xparo_properties_path = os.path.join(self.xparo_folder,'config',"properties.txt")
        self.xparo_custom_behaviors_folder_path = os.path.join(self.xparo_folder,'custom_behaviors')
        self.xparo_custom_files_folder_path = os.path.join(self.xparo_folder,'custom_files')
        self.xparo_custom_evns_folder_path = os.path.join(self.xparo_folder,'custom_envs')
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
        

        website_url = ("http" if local else "https") + '://'+('127.0.0.1:8000' if local else 'xparo.in')
        socket_url = ("ws" if local else "wss") + '://'+('127.0.0.1:8000' if local else 'xparo.in')
        self.website_full_url = website_url +'/chatbot_api/'+secret_key+'/'+project_id+'/'
        self.socket_full_url = socket_url + '/ws/chatbot_api/'+str(secret_key)+'/'+str(project_id)+'/'


        self.local_database = XP_Database(xparo_database_size,
                                            xparo_database_path,
                                            website_url,
                                            self.BAG_DIR,self.record_bags)
        try:
            threading.Thread(target=self._logging_update_loop, daemon=True).start()
        except:
            print("you are offline")



    def _logging_update_loop(self):
        while not self.local_database._stop_updates:
            time.sleep(self.local_database.update_interval)
            if self.local_database.session_id:
                self.local_database.update_logging_session(self.private_send)

    #########################################################################################
    def connect(self):
        print('''

        connencting to ...
        ██╗░░██╗██████╗░░█████╗░██████╗░░█████╗░
        ╚██╗██╔╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗
        ░╚███╔╝░██████╔╝███████║██████╔╝██║░░██║
        ░██╔██╗░██╔═══╝░██╔══██║██╔══██╗██║░░██║
        ██╔╝╚██╗██║░░░░░██║░░██║██║░░██║╚█████╔╝
        ╚═╝░░╚═╝╚═╝░░░░░╚═╝░░╚═╝╚═╝░░╚═╝░╚════╝░

        ''')
        print("the AI Engine")
        if self.connection_type=="websocket":
            if not self.websocket_connected:
                self.ws = Xparo_socket(str(self.socket_full_url),
                                on_message=self.on_ws_message,
                                on_error=self.on_ws_error,
                                on_open=self.on_ws_open,
                                on_close=self.on_ws_close,
                                )
                self.websocket_connected = True
                threading.Thread(target=self.ws.run_forever).start()
            else:
                print("already connected to xparo remote")
        elif self.connection_type=="rest":
            response = requests.get(self.website_full_url)
            if response.status_code == 201:
                data = response.json()
                self.on_ws_message('self.ws',data)
                threading.Thread(target=self.start_reset_framework).start()
            else:
                print("no response")
        elif self.connection_type=="offline":
            print("offline mode with custom llm is comming soon...")
        ###################################################
        #### getting initial data..

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



    def private_send(self,message,command_for=None):
        # print(f"command reviev for brain {message} and type is {command_for}")
        if not command_for:
            command_for=self.connection_type
        try:
            if command_for=="websocket":
                self.ws.send(message)
            elif command_for=="rest":
                response = requests.post(self.website_full_url, data=message,headers={'Content-type': 'application/json'})
                if response.status_code == 201:
                    self.on_ws_message('rest', response.json())
                    return True
                else:
                    print(str(response))
            elif command_for=="offline":
                pass
        except Exception as e:
            print(e)
            if command_for!="rest":
                # The socket may have died without a clean close frame (common on
                # flaky networks) -- on_ws_close never fires, so websocket_connected
                # stays incorrectly True and connect() would otherwise no-op forever.
                self.websocket_connected = False
                self.connect()

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
            elif k=="eval":
                return eval(val)
            elif k=="get_initial_local_env_data":
                self.get_initial_local_env_data()
            elif k=="sync_local_database":
                self.get_local_files()
            elif k=="log_updated":
                self.local_database.dashboard_receive({"log_updated":val},self.private_send)
                self.local_database._stop_updates = False
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




    def on_ws_error(self, ws, error):
        print(error)
        print(f'''
        Truble shooting:
            1. check your internet connection
            2. if that not working download latest version of xparo or from github = https://github.com/lazyxcientist/xparo
            3. try to switch to websocket connection or rest framework
        ''')

    def on_ws_open(self, ws, *args):
        self.websocket_connected = True
        print('''
        \\\\Connection Sussessfull//
           \\\\X.P.A.R.O remote//
            \\\\is 🄻🄸🅅🄴 now//
        ''')
        self.send_initial_data()
        
    def send_initial_data(self):
        # self.private_send(json.dumps({"initisilaze_api":{}}))
        self.local_database.dashboard_receive({"needed_robot_data":{"sent":True}},self.private_send)

    def on_ws_close(self, ws, *args):
        self.websocket_connected = False
        print('''

            xparo brain is
        █▀▀ █── █▀▀█ █▀▀ █▀▀ █▀▀▄ 
        █── █── █──█ ▀▀█ █▀▀ █──█ 
        ▀▀▀ ▀▀▀ ▀▀▀▀ ▀▀▀ ▀▀▀ ▀▀▀─
            retry again !!!

        ''')

    def start_reset_framework(self):
        print("starting reset framework")
        check = self.private_send(json.dumps({"initiliaze":True}))
        if check:
            while True:
                response = requests.get(self.website_full_url)
                if response.status_code == 201:
                    data = response.json()
                    self.on_ws_message('self.ws',data)
                time.sleep(0.2)
        else:
            print("unable to connect with X.P.A.R.O server")
            self.on_ws_close('self.ws')

    ##################################
    ###### function override #########
    ##################################

    def call_message(self,message,**kwargs):
        print(f"fun waiting to overrite :- {message}")


    #################################################################################







if __name__ == "__main__":
    ai_brain = Engine("secret_key","project_id")
