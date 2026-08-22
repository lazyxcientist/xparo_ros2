#!/usr/bin/env python3
import json
import os
from datetime import datetime
import uuid
import platform
import socket
import psutil  # To get hardware specs
import requests
import subprocess
import glob
import urllib.request
import ipaddress
import signal
from threading import Thread
from .blackbox_manager import BlackboxOrchestrator


class XP_Database():
    def __init__(self, xparo_database_size, xparo_database_path,xparo_website_url,BAG_DIR,record_bags,rosbag_control=None):
        # Parameters
        self.xparo_database_size = xparo_database_size  # Size in MB
        self.xparo_database_path = xparo_database_path
        self.unique_id = self.get_device_uuid()
        # self.xparo_website_url = ""

        ################################################
        # Track where we left off reading ROS2 logs to prevent sending duplicate data
        self.log_pointers_file = os.path.join(self.xparo_database_path, "log_pointers.json")
        print(f"Device ID: {self.unique_id}")
        self.session_id = None
        self.session_start_time = None
        self.resource_samples = []          # list of (timestamp, snapshot)
        self.last_update_time = None
        self.update_interval = 120            # seconds
        self._stop_updates = True
        ###################################################


        ###################################################
        self.orchestrator = None
        if record_bags:
            self.orchestrator = BlackboxOrchestrator(self.unique_id,xparo_website_url,BAG_DIR,rosbag_control)
            
            
            def handle_exit(signum, frame):
                self.orchestrator.running = False
                self.orchestrator.stop_recording()
                # Deliberately NOT exit(0): that raises SystemExit, a sibling
                # of KeyboardInterrupt rather than the same exception, so
                # xparo_ros.py's `except KeyboardInterrupt: pass` around
                # rclpy.spin() never caught it -- the process exited straight
                # from here, before main() ever reached
                # stop_logging_session(), which is the only thing that sends
                # this session's "session_end"/session_end_time update to
                # Django. Raising KeyboardInterrupt instead (the same
                # exception Python's own default SIGINT handler raises) lets
                # rclpy.spin() unwind normally into that existing shutdown
                # path for both SIGINT and SIGTERM (e.g. a `docker stop`/
                # systemd stop), so the final update actually gets sent.
                raise KeyboardInterrupt()
            signal.signal(signal.SIGINT, handle_exit)
            signal.signal(signal.SIGTERM, handle_exit)

            # Run manager in background
            manager_thread = Thread(target=self.orchestrator.manage_disk_and_upload, daemon=True)
            manager_thread.start()

            # Deliberately NOT calling self.orchestrator.start_recording()
            # here anymore -- RosbagControl's own __init__ already runs a
            # boot sequence (close whatever throwaway session the recorder
            # auto-opened at its own process launch, then decide for
            # itself whether/when to auto-start, per rosbag_start_mode/
            # rosbag_start_delay_seconds) the moment it's constructed,
            # which happens moments before this. Calling start_recording()
            # unconditionally here too raced that sequence -- both are
            # async (nothing blocks for rclpy to start spinning yet), so
            # whichever one's Resume/Stop service call happened to land
            # last on the recorder silently won, with no error anywhere.
            # Confirmed live: a real boot with record_bags:=true opened a
            # session, resumed it, and then had it immediately closed and
            # compressed again by the constructor's own always-run
            # cleanup -- the exact symptom of this race. See
            # rosbag_control.py's __init__/_boot_sequence.

            # while orchestrator.running:
            #     time.sleep(1)
        ###################################################



    def update_logging_session(self, private_send):
        """Send incremental updates for the current session -- merged into
        the SAME Logs_history row session_id already points at
        (Django's UPDATE_Logs_history_database, not another ADD), so a
        long-running session accumulates one row's worth of history, not
        one row per check-in."""
        if not self.session_id:
            print("No active logging session to update.")
            return

        new_logs = self.get_ros2_jazzy_logs()

        # Take a resource snapshot -- appended to resource_samples so
        # _average_resource_samples can fold it into a whole-session
        # average, both here (a running total-so-far) and again, finally,
        # in stop_logging_session's own session-end update.
        current_resources = self.get_smart_resource_consumption()
        self.resource_samples.append((datetime.utcnow(), current_resources))

        payload = {
            "id": self.session_id,
            "log_files": new_logs,
            "resources_consuption": current_resources,
            "extra_info": {
                "last_update": datetime.utcnow().isoformat(),
                "running_average": self._average_resource_samples(),
            }
        }

        private_send(json.dumps({"UPDATE_Logs_history_database": payload}), command_for="rest")

        self.last_update_time = datetime.utcnow()

    def stop_logging_session(self, private_send):
        """Send a final update -- total runtime and the average resource
        consumption across every sample taken this session -- then clear
        session_id so the *next* real connection (a genuinely new process
        life) starts a fresh session rather than silently reusing this
        one's id.

        Best-effort only: this is reachable from xparo_ros.py's
        KeyboardInterrupt handler (a graceful Ctrl+C/`ros2 launch` stop),
        not from a genuine force-kill or crash -- there's no signal to
        catch in that case, the exact same honest limitation
        blackbox_manager.py's own force-exit handling already documents
        for rosbag sessions that never got a clean Stop() either.
        """
        if not self.session_id:
            return
        uptime_seconds = (datetime.utcnow() - self.session_start_time).total_seconds()
        final_payload = {
            "id": self.session_id,
            "extra_info": {
                "event": "session_end",
                "session_end_time": datetime.utcnow().isoformat(),
                "total_runtime_seconds": uptime_seconds,
                **self._average_resource_samples(),
            }
        }
        private_send(json.dumps({"UPDATE_Logs_history_database": final_payload}), command_for="rest")
        self.session_id = None
        self.resource_samples = []

    ####################################################
    # Ensure the directory exists, create if not
    ####################################################
    def ensure_directory_exists(self, file_path):
        directory = os.path.dirname(file_path)
        if not os.path.exists(directory):
            os.makedirs(directory)

    ####################################################
    # Create file with default data if it doesn't exist
    ####################################################
    def load_or_create_file(self, file_path, default_data):
        self.ensure_directory_exists(file_path)
        if not os.path.exists(file_path):
            with open(file_path, 'w') as file:
                json.dump(default_data, file)

    ####################################################
    # Load data from file
    ####################################################
    def load_files(self):
        pth = os.path.join(self.xparo_database_path, "send_later.json")
        content = {}
        self.load_or_create_file(pth, content)
        with open(pth, 'r') as file:
            rtrn_data = json.load(file)
        return rtrn_data

    ####################################################
    # Process received dashboard message and handle commands
    ####################################################
    def dashboard_receive(self, data, private_send):
        try:
            for command, val in data.items():
                try:
                    if command == "ADD_Chat_prompts_database":
                        self.save_failed_request("ADD_Chat_prompts_database", val, private_send)
                    elif command == "ADD_Sensor_database":
                        self.save_failed_request("ADD_Sensor_database", val, private_send)
                    elif command == "ADD_Task_history_database":
                        self.save_failed_request("ADD_Task_history_database", val, private_send)
                    elif command == "ADD_Logs_history_database":
                        self.save_failed_request("ADD_Logs_history_database", val, private_send)
                    elif command == "task_updated":
                        if val:
                            self.remove_data(val)
                    elif command == "REST_API_TOKEN":
                        if self.orchestrator:
                            self.orchestrator.API_TOKEN = str(val).strip()
                            self.orchestrator._process_uploads()
                    elif command == "needed_robot_data":
                        if val:
                            self.send_robot_info(private_send)
                    elif command == "trigger_log_update":
                        if val:
                            self.trigger_log_update(private_send)
                    if command == "log_updated":
                        # This is the server confirming creation of a log entry
                        self.session_id = val   # store as current session ID
                        self.session_start_time = datetime.utcnow()
                        self.last_update_time = self.session_start_time
                        self.resource_samples = []   # reset samples for this session
                        print(f"Logging session started with ID: {self.session_id}")
                except Exception as e:
                    print(f"Error processing command '{command}': {e}")
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {e}")

    

    ####################################################
    # Function to send robot information
    ####################################################
    def send_robot_info(self, private_send):
        return_dict = {}
        # Width and Height handling
        width, height = self.get_display_resolution()
        return_dict['width'] = width
        return_dict['height'] = height
        # Get OS and IP information
        return_dict['os_name'] = platform.system()
        return_dict['os_version'] = platform.version()
        return_dict['os_release'] = platform.release()
        return_dict['hostname'] = socket.gethostname()
        return_dict['mac_address'] = ':'.join(['{:02x}'.format((uuid.getnode() >> ele) & 0xff) for ele in range(0,8*6,8)][::-1])
        # Local IP
        try:
            return_dict['ip_address'], return_dict['ip_source'] = self.get_best_ip()
        except socket.gaierror:
            return_dict['ip_address'] = "127.0.0.1"
            return_dict['ip_source'] = "localhost"
        # Hardware metrics
        return_dict['cpu_model'] = platform.processor()
        return_dict['cpu_count'] = psutil.cpu_count(logical=True)
        return_dict['cpu_physical_count'] = psutil.cpu_count(logical=False)
        memory_info = psutil.virtual_memory()
        return_dict['total_memory'] = f"{memory_info.total / (1024 ** 3):.2f} GB"
        return_dict['available_memory'] = f"{memory_info.available / (1024 ** 3):.2f} GB"

        disk_info = psutil.disk_usage('/')
        return_dict['total_disk'] = f"{disk_info.total / (1024 ** 3):.2f} GB"
        return_dict['used_disk'] = f"{disk_info.used / (1024 ** 3):.2f} GB"
        return_dict['free_disk'] = f"{disk_info.free / (1024 ** 3):.2f} GB"
        return_dict['device_id'] = self.unique_id
        # Network/Location data (Adding Public IP)
        return_dict["locations"] = {}
        return_dict["public_ip"] = None
        try:
            response = requests.get("http://ip-api.com/json/", timeout=5)
            location_data = response.json()
            return_dict['locations']['latitude'] = location_data.get('lat')
            return_dict['locations']['longitude'] = location_data.get('lon')
            return_dict['public_ip'] = location_data.get('query')
        except requests.RequestException:
            pass

        # Compile 'data' field with peripherals
        return_dict['data'] = {
            "peripherals": self.scan_hardware_peripherals(),
            "public_ip": return_dict["public_ip"] 
        }

        # No command_for override, unlike ROBOT_HEARTBEAT just below this --
        # this is deliberately sent over whichever transport is actually
        # active (websocket, when connection_type is "websocket"/"hybrid"-
        # connected), not forced over REST. REST's Project_dash(None, ...)
        # has no live consumer to work with (self.socket is None there), so
        # Django's ADD_robots_info handler can resolve/create the robot row
        # but can never join this connection to its 'robot'+id channel
        # group -- the exact group Manage_Dash.py's send_to_robot targets
        # for RUN_COMMAND/TELEOP/file-transfer. Forcing REST here meant
        # every websocket-connected robot that hadn't yet been issued (and
        # reconnected with) a RobotCredential could look fully "connected"
        # while every one of those silently reached nobody -- confirmed
        # live: group_send to a group with no members is a silent no-op.
        # Sending over the real transport lets the websocket-side handler
        # (which does have a live consumer) join the group as soon as
        # device_id resolves a robot row, exactly like a credentialed
        # connection already can from connect() itself. rest/hybrid-
        # fallback robots are unaffected -- _effective_transport() already
        # resolves this to "rest" for them, identical to before.
        private_send(json.dumps({"ADD_robots_info": return_dict}))

        # Starts a new logging session -- but only the first time in this
        # process's life, not on every call. send_initial_data (this
        # method's only caller, via dashboard_receive) is registered as
        # the transport's on_connected callback, which fires on every
        # reconnect (a WiFi hiccup, a Django/Daphne restart, a hybrid-mode
        # REST-fallback recovery -- none of them a new session by any
        # reasonable definition), not just true process startup.
        # Confirmed live: a robot that reconnected several times over one
        # otherwise-continuous run got a brand new Logs_history row every
        # single time -- "many logs" for what was really one session.
        # self.session_id is only ever set once Django actually confirms
        # the first ADD_Logs_history_database landed (see
        # dashboard_receive's "log_updated" handler), so a session that
        # never confirmed correctly still retries on the very next
        # reconnect rather than being silently lost.
        if self.session_id is None:
            self.trigger_log_update(private_send)

    def get_device_uuid(self):
        # Get MAC address (which is usually stable unless hardware changes)
        mac = ":".join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff)
                        for elements in range(0,2*6,2)][::-1])
        device_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, mac))  # Generate UUID based on MAC address
        return device_uuid
    
    def get_display_resolution(self):
        """Attempts to get actual physical display resolution from the frame buffer."""
        try:
            with open('/sys/class/graphics/fb0/virtual_size', 'r') as f:
                res = f.read().strip().split(',')
                return int(res[0]), int(res[1])
        except Exception:
            # Fallback if no display is connected or accessible
            return 900, 600

    def get_best_ip(self):
        """
        Attempts to find the best available IP address in this order:
        1. Tailscale/Overlay IP (100.64.x.x)
        2. True Public Internet IP
        3. Default Outbound Local IP
        4. Localhost (127.0.0.1)

        Returns a (ip, source) tuple -- `source` matches
        apps.analytics.models.Robots.IpSource's choices so Django can tell
        "Tailscale IP, mesh-reachable" from "public IP that's actually a NAT
        gateway, reachable by nobody" instead of just seeing a bare address.
        """

        # --- STEP 1: Look for a Tailscale / Overlay IP ---
        try:
            # Tailscale uses Carrier Grade NAT: 100.64.0.0 to 100.127.255.255
            ts_network = ipaddress.ip_network('100.64.0.0/10')

            for interface, snics in psutil.net_if_addrs().items():
                for snic in snics:
                    if snic.family == socket.AF_INET:  # We only want IPv4
                        ip = snic.address
                        if ipaddress.ip_address(ip) in ts_network:
                            return ip, "tailscale"  # Found the Tailscale IP!
        except Exception as e:
            print(f"Interface check failed: {e}")

        # --- STEP 2: Get True Public IP (Exposed to Internet) ---
        try:
            # We use a 3-second timeout so your script doesn't hang if the internet is down
            req = urllib.request.Request('https://api.ipify.org', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.read().decode('utf8').strip(), "public_unreachable"
        except Exception:
            pass # Internet might be down, move to next fallback

        # --- STEP 3: Get Default Outbound Local IP (Socket Trick) ---
        try:
            # This creates a dummy connection to figure out which IP the OS uses to route traffic out.
            # It does not actually send data, so it works even if 8.8.8.8 is unreachable.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(('8.8.8.8', 80))
                return s.getsockname()[0], "lan"
        except Exception:
            pass

        # --- STEP 4: Ultimate Fallback ---
        return "127.0.0.1", "localhost"

    def scan_hardware_peripherals(self):
        """Scans for connected USB, I2C, and UART devices."""
        peripherals = {"usb": [], "uart": [], "i2c": []}
        
        # Scan USB
        try:
            usb_out = subprocess.check_output(['lsusb'], text=True)
            for line in usb_out.strip().split('\n'):
                if line:
                    # Example line: Bus 001 Device 002: ID 8087:8000 Intel Corp.
                    parts = line.split("ID ")
                    if len(parts) > 1:
                        device_info = parts[1].split(" ", 1)
                        if len(device_info) == 2:
                            peripherals["usb"].append({"id": device_info[0], "name": device_info[1].strip()})
        except Exception as e:
            peripherals["usb"].append({"error": str(e)})

        # Scan UART / Serial
        try:
            uarts = glob.glob('/dev/serial/by-id/*') + glob.glob('/dev/serial/by-path/*') + glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyTHS*') + glob.glob('/dev/ttyS*')
            peripherals["uart"] = [{"path": u} for u in uarts]
        except Exception:
            pass

        # Scan I2C
        try:
            i2cs = glob.glob('/dev/i2c-*')
            peripherals["i2c"] = [{"path": i} for i in i2cs]
        except Exception:
            pass

        return peripherals
    
    def get_smart_resource_consumption(self):
        """Calculates an intelligent average of resource consumption. Every
        call here is also what update_logging_session appends to
        resource_samples -- see _average_resource_samples for how those
        turn into a whole-session average."""
        cpu_percentages = psutil.cpu_percent(interval=1, percpu=True)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        return {
            "cpu_avg_percent": sum(cpu_percentages) / len(cpu_percentages) if cpu_percentages else 0,
            "cpu_cores_percent": cpu_percentages,
            "ram_used_percent": ram.percent,
            "ram_available_mb": ram.available / (1024 * 1024),
            "disk_used_percent": disk.percent,
            "gpu_percent": self.get_gpu_percent(),
            "disk_io": psutil.disk_io_counters()._asdict() if psutil.disk_io_counters() else {},
            "net_io": psutil.net_io_counters()._asdict() if psutil.net_io_counters() else {}
        }

    def get_gpu_percent(self):
        """Best-effort NVIDIA GPU utilization via nvidia-smi -- covers
        desktop/server NVIDIA GPUs. Returns None (never a faked 0) when
        nvidia-smi isn't installed or there's no GPU, which is the honest,
        common case on this project's actual target hardware -- Jetson
        boards use tegrastats instead, a genuinely different tool this
        doesn't attempt to support yet. Matches this codebase's own
        precedent for hardware it can't really see into rather than
        faking a number (Phase 10's stub BT leaf nodes, xparo's bt_engine).
        """
        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode != 0:
                return None
            first_line = result.stdout.strip().splitlines()[0]
            return float(first_line)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
            return None

    def _average_resource_samples(self):
        """Averages every resource_samples snapshot collected since
        session start (get_smart_resource_consumption(), appended by
        update_logging_session every update_interval seconds). cpu/ram/
        disk are always present in a real snapshot; gpu_percent is
        averaged only over the samples where a GPU was actually detected,
        and left out entirely (never defaulted to 0) if none ever was --
        "no GPU seen" and "GPU idle at 0%" are different facts."""
        if not self.resource_samples:
            return {}
        cpu_values = [snapshot.get('cpu_avg_percent', 0) for _, snapshot in self.resource_samples]
        ram_values = [snapshot.get('ram_used_percent', 0) for _, snapshot in self.resource_samples]
        disk_values = [snapshot.get('disk_used_percent', 0) for _, snapshot in self.resource_samples]
        gpu_values = [
            snapshot['gpu_percent'] for _, snapshot in self.resource_samples
            if snapshot.get('gpu_percent') is not None
        ]

        averages = {
            "avg_cpu_percent": sum(cpu_values) / len(cpu_values),
            "avg_ram_percent": sum(ram_values) / len(ram_values),
            "avg_disk_percent": sum(disk_values) / len(disk_values),
            "sample_count": len(self.resource_samples),
        }
        if gpu_values:
            averages["avg_gpu_percent"] = sum(gpu_values) / len(gpu_values)
        return averages

    def get_ros2_jazzy_logs(self):
        """Fetches new lines from the latest ROS2 log directory."""
        log_base_dir = os.path.expanduser('~/.ros/log/')
        new_logs = []
        
        if not os.path.exists(log_base_dir):
            return {"error": "ROS2 log directory not found."}
            
        # Get the most recently modified directory in the ROS log folder
        subdirs = [os.path.join(log_base_dir, d) for d in os.listdir(log_base_dir) if os.path.isdir(os.path.join(log_base_dir, d))]
        if not subdirs:
            return {"error": "No ROS2 logs found."}
            
        latest_dir = max(subdirs, key=os.path.getmtime)
        rosout_file = os.path.join(latest_dir, 'rosout.log')
        
        if os.path.exists(rosout_file):
            # Load pointers to know where we stopped reading last time
            pointers = {}
            if os.path.exists(self.log_pointers_file):
                with open(self.log_pointers_file, 'r') as f:
                    pointers = json.load(f)
            
            last_position = pointers.get(rosout_file, 0)
            
            with open(rosout_file, 'r') as f:
                f.seek(last_position)
                new_logs = f.readlines()
                pointers[rosout_file] = f.tell() # Save new position
                
            with open(self.log_pointers_file, 'w') as f:
                json.dump(pointers, f)

        return {"log_directory": latest_dir, "new_entries": new_logs}

    def trigger_log_update(self, private_send):
        """Gathers ROS2 logs and resource metrics, then packages them for
        Django. The ONE call, per process life, that creates a new
        Logs_history row (send_robot_info's session_id guard ensures
        that) -- every later update within the same session goes through
        update_logging_session/stop_logging_session instead, which update
        this same row rather than creating new ones."""
        now = datetime.utcnow()
        payload = {
            "log_files": self.get_ros2_jazzy_logs(),
            "resources_consuption": self.get_smart_resource_consumption(),
            "extra_info": {"event": "session_start", "session_start_time": now.isoformat()},
            "created_at": now.isoformat(),
            "unique_id":self.unique_id,
        }
        # Send data to the server
        private_send(json.dumps({"ADD_Logs_history_database": payload}), command_for="rest")
    ####################################################
    # Save failed requests to the JSON file
    ####################################################
    def save_failed_request(self,data_type, payload, private_send):
        data = self.load_files()  # Load current data
        self.manage_file_size()  # Manage file size before adding

        payload["created_at"] = datetime.utcnow().isoformat()  # Add created_at
        task_id = str(uuid.uuid4())  # Generate a unique ID

        # Update the data with the new task
        pth = os.path.join(self.xparo_database_path, "send_later.json")
        data[task_id] = payload

        # Save the updated data to file
        with open(pth, 'w') as file:
            json.dump(data, file)

        # Try to send the payload
        try:
            payload["task_id"] = task_id  # Assign the unique task ID
            payload["unique_id"] = self.unique_id  # Assign the unique task ID
            private_send(json.dumps({data_type:payload}), command_for="rest")
        except Exception as e:
            print(f"Failed to send payload: {e}")
            # Leave it in the file for later retry

    ####################################################
    # Manage file size (remove oldest entries if size exceeds limit)
    ####################################################
    def manage_file_size(self):
        max_size = self.xparo_database_size * 1024 * 1024  # Convert MB to bytes
        pth = os.path.join(self.xparo_database_path, "send_later.json")

        if os.path.exists(pth):
            while os.path.getsize(pth) > max_size:
                data = self.load_files()

                # Sort failed requests by created_at
                if data:
                    sorted_requests = sorted(data.items(), key=lambda x: x[1].get("created_at"))
                    oldest_task_id, _ = sorted_requests[0]
                    
                    # Remove the oldest entry
                    del data[oldest_task_id]

                    # Write the updated file back
                    with open(pth, 'w') as file:
                        json.dump(data, file)
                else:
                    break  # If no more data, stop trimming

    ####################################################
    # Remove task from the file by task ID
    ####################################################
    def remove_data(self, task_id):
        data = self.load_files()  # Load current data
        if task_id in data:
            del data[task_id]  # Remove the task
            pth = os.path.join(self.xparo_database_path, "send_later.json")
            with open(pth, 'w') as file:
                json.dump(data, file)  # Save updated data
        else:
            print(f"Task ID {task_id} not found.")

