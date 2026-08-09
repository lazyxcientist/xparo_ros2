import os
import time
import shutil
import signal
import glob
import requests
from threading import Thread
from datetime import datetime

from .rosbag_control import PAUSED, WRITING

# --- CONFIGURATION ---


class BlackboxOrchestrator:
    """Disk lifecycle + cloud upload -- recording itself is delegated to
    RosbagControl (rosbag_control.py, a genuinely more robust strategy
    ported from tethered_module: verify-after-action, retry-with-backoff,
    a watchdog, and actual knowledge of whether a session is open/writing
    via the native rosbag2_recorder service interface) rather than the old
    subprocess.Popen("ros2 bag record", ...) wrapper, which had no way to
    tell a died/hung recorder from a healthy one.
    """

    def __init__(self,ROBOT_ID,xparo_website_url,BAG_DIR,rosbag_control=None):
        self.running = True
        self.ROBOT_ID = ROBOT_ID
        # Empty until a REST_API_TOKEN dispatch key arrives (see
        # engine.py's on_ws_message) -- manage_disk_and_upload below treats
        # an empty token as "not armed yet" and skips uploads until then.
        self.API_TOKEN = ""
        self.xparo_website_url = xparo_website_url
        self.BAG_DIR = BAG_DIR
        # None when there's no live rclpy node to drive recording through
        # (e.g. Engine constructed standalone/under test, outside
        # xparo_ros.py) -- start_recording()/stop_recording() become no-ops
        # in that case rather than crashing, same spirit as record_bags
        # itself being an opt-in.
        self.rosbag_control = rosbag_control
        # Production Thresholds
        self.DISK_MAX_PCT = 90.0
        self.DISK_TARGET_PCT = 70.0
        self.CHECK_INTERVAL = 15  # Faster checks for testing

        if not os.path.exists(self.BAG_DIR):
            os.makedirs(self.BAG_DIR, exist_ok=True)

        # --- INITIAL LOAD SYNC ---
        # Run one cleanup and upload cycle before starting the new recorder
        print("[INFO] Initial sync: Checking for unsent logs...")
        # self._process_uploads()

    def start_recording(self):
        if self.rosbag_control is None:
            print("[WARN] No RosbagControl available -- cannot start recording "
                  "(Engine isn't running inside a live rclpy node).")
            return
        print("[INFO] Starting ROS 2 Recorder via RosbagControl...")
        self.rosbag_control.handle_start()

    def stop_recording(self):
        if self.rosbag_control is None:
            return
        print("[INFO] Stopping ROS 2 Recorder via RosbagControl...")
        self.rosbag_control.handle_stop()

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
        
        # If a session is currently open (writing or merely paused -- either
        # way its .mcap is still being appended to), ignore the absolute
        # newest file. If it's the initial sync and nothing is recording
        # yet, we can try all.
        session_open = self.rosbag_control is not None and self.rosbag_control.state in (WRITING, PAUSED)
        target_bags = all_bags[:-1] if session_open else all_bags

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