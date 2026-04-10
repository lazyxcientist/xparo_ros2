import os
import time
import shutil
import signal
import subprocess
import yaml
import glob
import requests
from threading import Thread
from datetime import datetime

# --- CONFIGURATION ---


class BlackboxOrchestrator:
    def __init__(self,ROBOT_ID,xparo_website_url,BAG_DIR):
        self.recorder_process = None
        self.running = True
        self.ROBOT_ID = ROBOT_ID
        self.API_TOKEN ="2ec6003e332a15a5a70bee22e8e1a14218f14f67"
        self.xparo_website_url = xparo_website_url
        self.BAG_DIR = BAG_DIR
        self.CONFIG_PATH = os.path.join(self.BAG_DIR, "blackbox_record.yaml")
        # Production Thresholds
        self.DISK_MAX_PCT = 90.0
        self.DISK_TARGET_PCT = 70.0
        self.CHECK_INTERVAL = 15  # Faster checks for testing
        
        if not os.path.exists(self.BAG_DIR):
            os.makedirs(self.BAG_DIR, exist_ok=True)
            
        self._ensure_config_exists()
        
        # --- INITIAL LOAD SYNC ---
        # Run one cleanup and upload cycle before starting the new recorder
        print("[INFO] Initial sync: Checking for unsent logs...")
        # self._process_uploads()

    def _ensure_config_exists(self):
        """Creates a configuration with 1MB limits for testing."""
        if not os.path.exists(self.CONFIG_PATH):
            config = {
                "storage": "mcap",
                "max_bag_size": 104857600, ### 100mb ,,  ##1048576, # 1 MB for testing
                "compression_format": "zstd",
                # "topics": ["/tf", "/tf_static", "/cmd_vel", "/diagnostics", "/odom"]
            }
            with open(self.CONFIG_PATH, 'w') as f:
                yaml.dump(config, f)
            print(f"[INFO] Created test config (1MB limit) at {self.CONFIG_PATH}")

    def start_recording(self):
        """Builds a robust, timestamped recording command."""
        print("[INFO] Starting ROS 2 Recorder...")
        
        with open(self.CONFIG_PATH, 'r') as f:
            cfg = yaml.safe_load(f)

        # FIX: Generate a unique folder name using timestamp to avoid [ERROR] folder exists
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(self.BAG_DIR, f"log_{timestamp}")

        cmd = [
            "ros2", "bag", "record",
            "-o", output_path,
            "--storage", cfg.get("storage", "mcap"),
            "--max-bag-size", str(cfg.get("max_bag_size", 1048576)),
            "--compression-mode", "file",
            "--compression-format", cfg.get("compression_format", "zstd"),
        ]

        topics = cfg.get("topics", [])
        if topics:
            cmd.extend(topics)
        else:
            cmd.append("--all")

        print(f"[EXEC] {' '.join(cmd)}")
        self.recorder_process = subprocess.Popen(
            cmd, preexec_fn=os.setsid, stdout=subprocess.DEVNULL
        )

    def stop_recording(self):
        if self.recorder_process:
            print("[INFO] Stopping ROS 2 Recorder...")
            try:
                # Kill the entire process group gracefully
                os.killpg(os.getpgid(self.recorder_process.pid), signal.SIGINT)
                self.recorder_process.wait(timeout=10)
            except Exception as e:
                print(f"[ERROR] Shutdown error: {e}")
            print("[INFO] Recorder stopped.")

    def get_disk_usage(self):
        stats = shutil.disk_usage(self.BAG_DIR)
        return (stats.used / stats.total) * 100

    def manage_disk_and_upload(self):
        """Background thread to manage disk and sync to Django/Azure."""
        while self.running:
            usage = self.get_disk_usage()
            if usage > self.DISK_MAX_PCT:
                self._force_cleanup()
            # self._process_uploads()
            # 2. CHECK TOKEN BEFORE UPLOADING
            if not self.API_TOKEN or self.API_TOKEN == "":
                print("[MANAGER THREAD] Idle: Waiting for API_TOKEN from server...")
            else:
                self._process_uploads()
                
            time.sleep(self.CHECK_INTERVAL)

    def _force_cleanup(self):
        # Finds all mcap files recursively
        files = glob.glob(os.path.join(self.BAG_DIR, "**/*.mcap"), recursive=True)
        files.sort(key=os.path.getctime)
        for f in files:
            if self.get_disk_usage() < self.DISK_TARGET_PCT: break
            os.remove(f)
            # Remove empty parent directory if it was a rosbag folder
            parent = os.path.dirname(f)
            if not os.listdir(parent):
                os.rmdir(parent)
            print(f"[CLEANUP] Disk threshold met. Deleted: {f}")

    def _process_uploads(self):
        """Finds completed bags and uploads to Django API."""
        # Find all MCAP files in subfolders
        all_bags = glob.glob(os.path.join(self.BAG_DIR, "**/*.mcap"), recursive=True)
        
        # Sort by modification time (oldest first)
        all_bags.sort(key=os.path.getmtime)
        
        # If the recorder is running, ignore the absolute newest file (active file)
        # If it's the initial sync and recorder isn't started yet, we can try all.
        target_bags = all_bags[:-1] if self.recorder_process else all_bags

        if not target_bags:
            return

        for bag_path in target_bags:
            try:
                # Use os.path.basename for cleaner log output
                filename = os.path.basename(bag_path)
                print(f"[UPLOAD] Attempting: {filename}")
                
                with open(bag_path, 'rb') as f:
                    payload = {
                        'robot_id': self.ROBOT_ID,
                        'data': '{}', 
                        'sensor_type': 'rosbag'
                    }
                    headers = {'Authorization': f'Token {self.API_TOKEN}'}
                    
                    response = requests.post(
                        self.xparo_website_url+"/api/upload_rosbag/", 
                        files={'bag_file': f}, 
                        data=payload, 
                        headers=headers, 
                        timeout=300
                    )
                    
                    # 201 is DRF's default for 'Created'
                    if response.status_code in [200, 201]:
                        os.remove(bag_path)
                        # Clean up empty folder left behind by rosbag
                        parent_dir = os.path.dirname(bag_path)
                        if not os.listdir(parent_dir):
                            os.rmdir(parent_dir)
                            
                        print(f"[SUCCESS] Server confirmed receipt. Deleted: {filename}")
                    else:
                        print(f"[FAILED] Server status {response.status_code}: {response.text}")
            except Exception as e:
                print(f"[NETWORK ERROR] Could not reach server: {e}")
                break # Stop loop to retry next interval

def handle_exit(signum, frame):
    orchestrator.running = False
    orchestrator.stop_recording()
    exit(0)

if __name__ == "__main__":
    orchestrator = BlackboxOrchestrator(
        ROBOT_ID="827a8f7f-0875-533e-8759-5e56971072a3"
        ,xparo_website_url="http://127.0.0.1:8000"
        ,BAG_DIR='/home/scientist/Documents/nave/xparo/d6f86221-67c4-4068-9ac4-05c95e9b5ca9/ros_bags'
    )
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    # Run manager in background
    manager_thread = Thread(target=orchestrator.manage_disk_and_upload, daemon=True)
    manager_thread.start()

    # Start recorder in main thread
    orchestrator.start_recording()
    
    while orchestrator.running:
        time.sleep(1)