import json
import re
import threading
import time
import os
import psutil
from datetime import datetime
from .database import XP_Database
from .transports.django_ws import DjangoWsTransport
from . import remote_ops
from . import connectivity
from .bt_engine import run_task
from .bt_engine import plugin_loader
from .bt_engine import runners
from . import sync_hash

record_bags = False
xparo_database_size =  80

# Pairs with apps.analytics.models.ROBOT_ONLINE_THRESHOLD_SECONDS (90s) on
# the Django side -- sending every 30s tolerates one missed beat before a
# robot flips to "offline" there.
HEARTBEAT_INTERVAL_SECONDS = 30

# Health & Errors popup: problems go to the dashboard in batches this far
# apart (xparo_ros.py's timer), and a "Watch live" request lasts this long
# unless the popup renews it.
HEALTH_FLUSH_SEC = 2.0
HEALTH_WATCH_SEC = 600.0

# Status mapping
STATUS_MAP = {
    "IDLE": 0,
    "RUNNING": 1,
    "SUCCESS": 2,
    "FAILURE": 3,
    # Add more if needed
}


NO_EXECUTOR_ERROR = (
    "This robot's XPARO process is running without its ROS 2 node, so it can't run behaviour trees. "
    "Start XPARO on the robot with its ROS 2 launch file (ros2 launch xparo xparo_launch.py)."
)


class Engine():
    def __init__(self,secret_key,project_id,connection_type = "websocket",record_bags=record_bags,BAG_DIR=None,environment=None,rosbag_control=None,joy_publish=None,xparo_transport="django_ws",tethered_channels_config=None,xparo_stage="production"):
        global xparo_database_size
        self.xparo_folder = os.path.abspath(os.path.join( os.path.dirname(__file__), os.pardir))
        # Finding F2 (MEDIUM): tmp_folder used to be the bare relative path
        # "." -- xparo_database_path (log_pointers.json, the send_later
        # outbox) and BAG_DIR ended up wherever the process happened to be
        # launched from (systemd's default "/", a different cwd from a
        # manual terminal run vs. a ros2 launch, etc). Depending on the
        # LAUNCHING SHELL'S cwd for where persistent robot state lives means
        # the same robot can silently start reading/writing a completely
        # different log_pointers.json / send_later.json / bag directory on
        # its next restart -- confirmed live by starting the same Engine
        # from two different working directories and observing two disjoint
        # "./xparo/<project_id>/" trees. self.xparo_folder (the installed
        # package's own location, already used below for every other
        # per-robot data path -- transfer_dir, custom_behaviors, etc) is
        # absolute and independent of the caller's cwd, so it's used here
        # too instead of a fresh ad-hoc relative base.
        self.tmp_folder = self.xparo_folder
        xparo_database_path = os.path.join(self.tmp_folder,"xparo",project_id,'database')
        # BAG_DIR must arrive through the constructor, same reasoning as
        # record_bags just below -- XP_Database (and, if recording, the
        # BlackboxOrchestrator it starts) is built before __init__ returns,
        # so a caller setting self.BAG_DIR afterward (as xparo_ros.py used
        # to) has no effect on where bags actually get written.
        self.BAG_DIR = BAG_DIR or os.path.join(self.tmp_folder,"xparo",project_id,'ros_bags')
        self.connection_type = connection_type #"websocket" # "rest" , "websocket" , "hybrid" , "offline"
        # joy_publish(axes, buttons) -> None -- publishing a real
        # sensor_msgs/Joy message needs an actual ROS2 node context, which
        # Engine deliberately doesn't depend on (it's usable standalone --
        # see the __main__ block at the bottom of this file). xparo_ros.py
        # supplies the real one; defaults to a no-op so TELEOP is a safe
        # no-op (still acks) rather than a crash outside a live node.
        self.joy_publish = joy_publish or (lambda axes, buttons: None)
        # bt_engine.executor.BehaviorTreeExecutor -- set post-construction
        # by xparo_ros.py, same reasoning as call_message/files just below
        # in Xparo.__init__: it needs this already-built Engine instance to
        # call add_live_update/add_task_history on, so it can't be built
        # (or passed in) before Engine exists. None here means RUN_TASK is
        # a safe no-op (matches TELEOP's own "no live node" default) rather
        # than an AttributeError, for Engine's standalone-outside-ROS2 use
        # (see this file's own __main__ block) and every test in this repo.
        self.bt_executor = None
        # ads.AdManager -- set up by setup_ads() (xparo_ros.py calls it
        # with the xparo_ads_display launch argument). None = this robot
        # shows no ads, and ads_schedule messages are ignored.
        self.ad_manager = None
        # Phase 13 -- XML tags sync_bt_inline_nodes most recently
        # registered, so a resync can unregister exactly those before
        # re-registering the current file set (load_plugins/register_plugins
        # only ever add/overwrite entries, never remove ones for a file
        # that's disappeared -- see sync_bt_inline_nodes' own docstring).
        self._inline_node_tags = set()
        # Same idea, for sync_custom_node_files' own registrations --
        # tracked separately from _inline_node_tags so the two sync
        # mechanisms never unregister tags the OTHER one most recently
        # loaded.
        self._custom_node_file_tags = set()
        # Base dir for LIST_FILES/DELETE_FILE/FILE_REQ -- deliberately
        # separate from BAG_DIR (rosbag sessions) and the xparo_* config
        # paths below (behavior trees/env/properties, a different concept).
        self.transfer_dir = os.path.join(self.xparo_folder, 'transferred_files')
        self.file_transfer = remote_ops.FileTransferSession(self.transfer_dir)

        # 2026-09-28 stress test finding F5 (HIGH): a task's own TASK_RESULT
        # and its ADD_Task_history_database record used to be one-shot,
        # fire-and-forget sends -- if the connection happened to be down
        # right when a task finished (confirmed live: kill the server while
        # a task is mid-run, bring it back), both were simply lost forever,
        # with no retry on reconnect. Queued here instead, flushed on every
        # successful (re)connection (send_initial_data, called via
        # on_connected) -- see _send_important_dict/_flush_pending_important_sends.
        self._pending_important_sends = []
        self._pending_important_sends_lock = threading.Lock()

        # GET_LIVE_STATUS's uptime field -- when this Engine (this robot's
        # connection/process) itself started, not any Django-tracked
        # logging session.
        self._engine_started_at = time.time()
        # Health & Errors / task feedback state -- see flush_health,
        # self_health_statuses and _task_started/_task_finished.
        self._health_watch_until = 0.0
        self._health_full_report_due = True
        self._running_task_info = {}
        self._last_task_outcome = None
        # on_task_event(kind, info), kind "started"|"finished" -- set by
        # xparo_ros.py to log and publish /xparo/task_result.
        self.on_task_event = None

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

        self.project_id = project_id
        # Kept for _fall_back_to_raw_secret below -- the one value that
        # must never get silently lost if a persisted credential turns out
        # to be stale.
        self._raw_secret_key = secret_key
        self._environment = environment
        # Read by run_task.py's ALLOWED_TASK_STAGES check before ticking
        # any RUN_TASK dispatch -- see xparo_ros.py's own declare_parameter
        # for the "production" default rationale.
        self.xparo_stage = xparo_stage
        persisted_credential = self._load_persisted_credential()
        effective_secret = persisted_credential or secret_key

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
                # Only give the transport a way to fall back if this
                # attempt is actually using a *persisted* credential, not
                # a raw xparo_secret_key -- see _fall_back_to_raw_secret's
                # own docstring for why the distinction matters.
                on_persisted_credential_rejected=(
                    self._fall_back_to_raw_secret if persisted_credential else None
                ),
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

        Scoped to self.project_id: a credential is only meaningful for the
        specific project it was issued under (ProjectSecretKey/
        RobotCredential rows both belong to one project), so a stale file
        left over from an earlier run against a *different* project_id
        must not silently override -- and mask -- a freshly-supplied
        secret_key for this one. Confirmed this exact failure mode for
        real: a leftover credential.json from an earlier local test kept
        getting used instead of a brand new xparo_secret_key launch
        argument for an unrelated project, producing a confusing 403 with
        no indication the supplied secret was never actually tried.
        """
        try:
            with open(self.xparo_credential_path, 'r') as file:
                stored = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if stored.get('project_id') != self.project_id:
            return None
        return stored.get('value') or None

    def _persist_credential(self, raw_value):
        os.makedirs(os.path.dirname(self.xparo_credential_path), exist_ok=True)
        # Finding F3 (LOW): this is the robot's live auth secret for the
        # Django server -- written with default open() permissions
        # (confirmed live: 0644, readable by any local user on the
        # machine), rather than restricted to the owner like an SSH key or
        # any other on-disk credential. os.open with mode=0o600 sets that
        # from the moment the file is created (no window where a default-
        # permission file briefly exists), and the umask can only narrow
        # it further, never widen it.
        fd = os.open(self.xparo_credential_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump({'value': raw_value, 'project_id': self.project_id}, file)

    def _fall_back_to_raw_secret(self):
        """Called at most once, from the transport's on_ws_error, the
        moment a persisted ROBOT_CREDENTIAL gets rejected with a 403 on its
        very first handshake attempt -- meaning the credential is no
        longer valid server-side (its Robots/RobotCredential row was
        deleted or rotated) even though this project_id still matches.
        _load_persisted_credential's own project-scoping check (see its
        docstring) only catches a stale file left over from a *different*
        project; it has no way to know a same-project credential has since
        been invalidated, and without this, run_forever(reconnect=N) would
        keep retrying that exact same doomed URL forever -- a perfectly
        valid xparo_secret_key argument would never actually get tried.

        Only wired up when this connection actually started from a
        persisted credential (see __init__) -- a real bad/revoked project
        secret typed by an operator has nothing to fall back to and should
        keep failing normally.
        """
        print("persisted ROBOT_CREDENTIAL was rejected (403) -- clearing it and retrying with the original secret_key")
        try:
            os.remove(self.xparo_credential_path)
        except FileNotFoundError:
            pass
        self.transport.close()
        self.transport = DjangoWsTransport(
            self._raw_secret_key, self.project_id,
            on_message=self.on_ws_message,
            on_connected=self.send_initial_data,
            connection_type=self.connection_type,
            environment=self._environment,
        )
        self.transport.connect()

    def _logging_update_loop(self):
        # Heartbeat runs unconditionally, every HEARTBEAT_INTERVAL_SECONDS,
        # independent of _stop_updates/session_id -- Robots.is_online must
        # not depend on a rosbag/logging session being active. The original
        # resource/log-session update keeps its own cadence and gating,
        # folded into the same loop rather than a second thread.
        seconds_since_resource_update = 0
        while True:
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)

            self.private_send(json.dumps(self._build_heartbeat_payload()), command_for="rest")

            seconds_since_resource_update += HEARTBEAT_INTERVAL_SECONDS
            if seconds_since_resource_update >= self.local_database.update_interval:
                seconds_since_resource_update = 0
                if not self.local_database._stop_updates and self.local_database.session_id:
                    self.local_database.update_logging_session(self.private_send)

    def _build_heartbeat_payload(self):
        """Extracted from _logging_update_loop so the diagnostics_level
        glue is directly testable without driving the loop's own
        time.sleep. Folded into the existing heartbeat rather than a
        separate message -- cheap (see _refresh_self_diagnostics' own
        docstring) and this is exactly what Phase B's alerting/attention-
        triage queue needs: a worsening level reaching Django without
        anyone needing a diagnostics popup open."""
        self._refresh_self_diagnostics()
        aggregator = self._get_diagnostics_aggregator()
        diagnostics_level = aggregator.snapshot()["overall_level"] if aggregator is not None else None
        return {"ROBOT_HEARTBEAT": {
            "device_id": self.local_database.unique_id,
            "diagnostics_level": diagnostics_level,
        }}

    def _get_problem_tracker(self):
        """health.ProblemTracker owned by the Xparo node (fed by /rosout
        and /diagnostics) -- same lookup/reasoning as _get_rosbag_control
        just below."""
        if self.bt_executor is None:
            return None
        return getattr(self.bt_executor.node, 'problem_tracker', None)

    def flush_health(self):
        """Called every HEALTH_FLUSH_SEC by xparo_ros.py. Sends problems
        that are new or recurred since the last flush as one batch (Django
        adds count_delta to the stored count), and the live component
        board while someone watches. Nothing is sent while the connection
        is down: the tracker keeps counting and everything goes out on the
        first flush after reconnecting."""
        tracker = self._get_problem_tracker()
        if tracker is None:
            return
        if self.connection_type in ("websocket", "hybrid") and not getattr(self.transport, 'websocket_connected', True) \
                and not getattr(self.transport, 'rest_fallback_active', False):
            return
        aggregator = self._get_diagnostics_aggregator()
        if aggregator is not None:
            aggregator.check_stale()
        full = self._health_full_report_due
        changes = tracker.flush(full=full)
        if changes or full:
            self._health_full_report_due = False
            self._send_dict({"PROBLEMS_REPORT": {
                "device_id": self.local_database.unique_id,
                # First report after each (re)connect: the complete set of
                # what's active now -- everything else is resolved.
                "full": full,
                "entries": [{
                    "source": c["source"], "name": c["name"], "level": c["level"], "message": c["message"],
                    "count_delta": c["count_delta"], "active": c["active"], "hardware_id": c["hardware_id"],
                } for c in changes],
            }})
        if aggregator is not None and time.monotonic() < self._health_watch_until:
            self._send_dict({"DIAGNOSTICS_SNAPSHOT": aggregator.snapshot()})

    def self_health_statuses(self):
        """xparo's own statuses ([(name, level, message)]) -- published on
        /diagnostics by xparo_ros.py every second, and recorded straight
        into the aggregator by _refresh_self_diagnostics when there's no
        live node publishing them."""
        from . import health
        rosbag_control = self._get_rosbag_control()
        rosbag = None if rosbag_control is None else {
            "alive": rosbag_control.recorder_alive, "state": rosbag_control.state,
            "owns_launch_process": getattr(rosbag_control, 'owns_launch_process', True),
        }
        try:
            disk_percent = psutil.disk_usage('/').percent
        except OSError:
            disk_percent = None
        mode = "tethered_tcp" if self.transport.__class__.__name__ == "TetheredTcpTransport" else self.connection_type
        connected = bool(getattr(self.transport, 'websocket_connected', True)) or \
            bool(getattr(self.transport, 'rest_fallback_active', False)) or mode == "rest"
        running = [info.get("task_title") or info.get("task_id") or "task" for info in self._running_task_info.values()]
        return health.self_statuses(
            {"mode": mode, "connected": connected}, rosbag,
            {"running": running, "last": self._last_task_outcome}, disk_percent,
        )

    # ---- task events: dashboard + ROS feedback ----------------------------
    def _task_started(self, payload):
        """on_started for run_task.handle_run_task."""
        info = payload.get("TASK_STARTED") or {}
        self._running_task_info[info.get("run_id")] = info
        self._send_dict(payload)
        self._emit_task_event("started", info)

    def _task_finished(self, payload):
        """send_response for run_task.handle_run_task: the final result
        must survive a connection outage (finding F5), so it's queued."""
        info = payload.get("TASK_RESULT") or {}
        started = self._running_task_info.pop(info.get("run_id"), {})
        title = info.get("task_title") or started.get("task_title") or info.get("task_id") or "task"
        self._last_task_outcome = {
            "title": title, "success": bool(info.get("success")),
            "explanation": info.get("explanation") or info.get("error") or "",
        }
        self._send_important_dict(payload)
        self._emit_task_event("finished", info)

    def _emit_task_event(self, kind, info):
        """Tells whoever runs this Engine (xparo_ros.py: ROS log +
        /xparo/task_result) -- before this, a task started from
        /xparo/run_task printed nothing at all on the robot, so from the
        ROS side it looked like it never ran."""
        callback = self.on_task_event
        if callback is None:
            return
        try:
            callback(kind, info)
        except Exception as e:
            print(f"[task] event hook failed: {e}")

    def _get_rosout_watcher(self):
        if self.bt_executor is None:
            return None
        return getattr(self.bt_executor.node, 'rosout_watcher', None)

    def _get_rosbag_control(self):
        """Same lookup sync_rosbag_config already uses -- Engine never
        keeps its own reference (bt_executor, and therefore its node,
        doesn't exist yet at Engine construction time), so every caller
        re-resolves it fresh, live, each time it's actually needed."""
        if self.bt_executor is None:
            return None
        return getattr(self.bt_executor.node, 'rosbag_control', None)

    def _get_diagnostics_aggregator(self):
        """Same lookup/reasoning as _get_rosbag_control just above."""
        if self.bt_executor is None:
            return None
        return getattr(self.bt_executor.node, 'diagnostics_aggregator', None)

    def _refresh_self_diagnostics(self):
        """xparo's own bootstrap DiagnosticStatus entries -- see
        diagnostics_aggregator.py's module docstring for why this exists
        (a robot with zero external /diagnostics publishers still gets a
        meaningful snapshot). Deliberately cheap (no blocking psutil call
        like get_smart_resource_consumption's 1s cpu_percent sample) since
        this runs on every 30s heartbeat, not just on an on-demand GET.
        """
        aggregator = self._get_diagnostics_aggregator()
        if aggregator is None:
            return
        # A missing rosbag recorder used to be recorded as 'error' even
        # when recording was simply off (record_bags:=false, the default),
        # so every such robot showed an overall "error" level on the fleet
        # page -- health.self_statuses only calls it an error when this
        # launch started a recorder itself.
        for name, level, message in self.self_health_statuses():
            aggregator.record_self_status(name, level, message)

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

    def _xparo_server_reachable(self):
        """WIFI_CONNECT's auto-revert check: can this robot reach the XPARO
        server over whatever network it's on now? A fresh HTTP request,
        not the websocket's state -- right after a network switch the old
        socket can still look connected while being dead."""
        base_url = getattr(self.transport, 'website_base_url', None)
        if not base_url:
            return True  # tethered_tcp: no server to reach, nothing to revert for
        return connectivity.http_reachable(base_url + '/')

    def _on_network_changed(self):
        reconnect = getattr(self.transport, 'force_reconnect', None)
        if reconnect is not None:
            reconnect()

    def _try_send_dict(self, payload):
        """Like _send_dict, but reports whether the send is likely to have
        actually gone anywhere -- used by _send_important_dict/
        _flush_pending_important_sends below to decide what to retry.
        websocket-client's own send() can raise outright on a KNOWN-dead
        socket (the common case, confirmed live as a "Broken pipe"), but a
        connection that's died without the client noticing yet may accept
        the write locally without it ever arriving -- transport.
        websocket_connected (see transports/django_ws.py's own docstring
        on exactly this) is the best signal already available in this
        codebase for "does this transport currently believe it's live",
        checked in addition to catching a raised exception, not instead of.
        """
        try:
            self.private_send(json.dumps(payload))
            return bool(getattr(self.transport, 'websocket_connected', True))
        except Exception as e:
            print(f"[Engine] send failed, queued for retry on reconnect: {e}")
            return False

    def _send_important_dict(self, payload):
        """For a message that must not be silently lost if the connection
        is down right now -- a task's final TASK_RESULT, primarily (see
        run_task.py's own use of this via the send_response callback
        engine.py wires up for RUN_TASK). Queues for retry instead of just
        failing once; _flush_pending_important_sends actually retries them,
        called on every successful (re)connection.
        """
        if self._try_send_dict(payload):
            return
        with self._pending_important_sends_lock:
            self._pending_important_sends.append(payload)

    def _flush_pending_important_sends(self):
        """Called once a connection is confirmed usable again
        (send_initial_data, via on_connected) -- retries everything queued
        by _send_important_dict/add_task_history while the connection was
        down. Whatever still can't be sent (a reconnect that itself drops
        again immediately) stays queued for the NEXT successful connect,
        in original order, rather than being dropped.
        """
        with self._pending_important_sends_lock:
            pending, self._pending_important_sends = self._pending_important_sends, []
        still_pending = [p for p in pending if not self._try_send_dict(p)]
        if still_pending:
            with self._pending_important_sends_lock:
                self._pending_important_sends = still_pending + self._pending_important_sends

    def send(self,message,remote_name="default"):
        filtered_data = json.dumps({"ask_bot_api":{"unique_id":self.local_database.unique_id, "data":{"from robot":"testing one.."},"question":message}})
        self.private_send(filtered_data,
                        #   command_for="rest"
                          )

    def add_task_history(self,message):
        # 2026-09-28 stress test finding F5: a task history record is
        # exactly the kind of thing that must not be silently lost if the
        # connection happens to be down at the moment a task finishes --
        # routed through _send_important_dict (queued + retried on the
        # next reconnect) instead of a one-shot private_send, unlike
        # add_live_update just below (a live nicety, fine to drop).
        payload = {"ADD_Task_history_database":{"unique_id":self.local_database.unique_id,
                                                                "input_data": message.get("input_data", {}),
                                                                "output_data": message.get("output_data", {}),
                                                                "type": message.get("type", "generic_task"),
                                                                "created_at": message.get("created_at", datetime.now().isoformat())
                                                                }}
        self._send_important_dict(payload)

    def add_live_update(self,message):
        filtered_data = json.dumps({"ADD_live_update_bt":{"unique_id":self.local_database.unique_id,

                                                                # Finding F8 (MEDIUM): this filter used to drop
                                                                # run_id entirely, so two tasks running at once
                                                                # produced indistinguishable live node-status
                                                                # updates on the wire -- the dashboard's live
                                                                # canvas had no way to tell them apart.
                                                                "run_id": message.get("run_id"),
                                                                # Which task/tree this status belongs to, so the
                                                                # Behaviour editor only lights up the tree that's
                                                                # actually running (absent for Nav2's own log).
                                                                "task_id": message.get("task_id"),
                                                                "tree_name": message.get("tree_name"),
                                                                "task_title": message.get("task_title"),
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

    def sync_bt_plugins(self, plugins=None):
        """Phase 12. `plugins` is Project_Dashboard.custom_bt_node_plugins's
        shape ([{"path":..., "enabled":...}]) fresh from Django when
        resyncing (persisted to disk here, same as custom_aiml/custom_maps
        above) -- None means "load whatever was persisted from a previous
        sync" (the startup case, called once from xparo_ros.py's
        __init__, before any RUN_TASK could possibly reference a plugin
        tag). Either way, ends by (re)registering every enabled path's
        tags into NODE_REGISTRY -- calling this twice with the same paths
        simply overwrites the same registry entries, not a problem.
        """
        pth = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'plugin_paths.json')
        if plugins is not None:
            content = json.dumps(plugins)
            self.local_database.load_or_create_file(pth, content)
            with open(pth, 'w') as file:
                file.write(content)
        else:
            if not os.path.exists(pth):
                return
            with open(pth, 'r') as file:
                try:
                    plugins = json.load(file)
                except json.JSONDecodeError:
                    plugins = []

        enabled_paths = [p.get('path') for p in plugins if isinstance(p, dict) and p.get('enabled') and p.get('path')]
        loaded = plugin_loader.register_plugins(enabled_paths)
        print(f"[bt_engine] loaded plugin tags: {list(loaded.keys())}")

    def sync_bt_inline_nodes(self, inline_code=None):
        """Phase 13. inline_code is Project_Dashboard.custom_bt_node_inline_code's
        shape ({node_name: python_source}) fresh from Django when
        resyncing -- persisted here as one file per node under
        custom_behaviors/inline_nodes/, then loaded through the exact same
        plugin_loader.register_plugins() Phase 12 built (inline code is
        just another set of .py paths on disk by the time it reaches the
        loader). None means "load whatever was persisted from a previous
        sync" (the startup case, called once from xparo_ros.py's
        __init__, mirroring sync_bt_plugins).

        Unlike custom_aiml/custom_maps/custom_bt_node_plugins above, a
        node removed from Django's dict has its on-disk file deleted here
        too rather than left in place -- CustomNodeCodeLog logs a
        "deleted" action for exactly this case on the Django side, and
        leaving the file behind would mean a project owner "deleting" a
        node has no actual effect on what can still run on the robot.

        Which node names currently exist is tracked via an explicit
        manifest file (same idea as plugin_paths.json above), not by
        os.listdir()-ing the folder -- an import can leave a __pycache__/
        entry behind next to the .py files, which a raw directory listing
        would otherwise hand to the loader as if it were a plugin path.
        """
        folder = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'inline_nodes')
        manifest_path = os.path.join(folder, 'manifest.json')
        if inline_code is not None:
            os.makedirs(folder, exist_ok=True)
            previous_names = set()
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r') as file:
                    try:
                        previous_names = set(json.load(file))
                    except json.JSONDecodeError:
                        previous_names = set()
            for stale_name in previous_names - set(inline_code):
                try:
                    os.remove(os.path.join(folder, stale_name + '.py'))
                except OSError:
                    pass
            for name, source in inline_code.items():
                with open(os.path.join(folder, name + '.py'), 'w') as file:
                    file.write(source)
            with open(manifest_path, 'w') as file:
                json.dump(list(inline_code.keys()), file)

        names = []
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as file:
                try:
                    names = json.load(file)
                except json.JSONDecodeError:
                    names = []
        paths = [os.path.join(folder, name + '.py') for name in names]
        # Unregister whatever this method itself most recently loaded
        # before reloading -- a file renamed or deleted between two syncs
        # must not leave its old tag runnable in NODE_REGISTRY (see
        # plugin_loader.unregister_tags' own docstring for why
        # register_plugins alone can't do this).
        plugin_loader.unregister_tags(self._inline_node_tags)
        loaded = plugin_loader.register_plugins(paths)
        self._inline_node_tags = set(loaded.keys())
        print(f"[bt_engine] loaded inline node tags: {list(loaded.keys())}")

    _CUSTOM_NODE_FILE_EXTENSIONS = {'python': '.py', 'cpp': '.cpp', 'javascript': '.js', 'bash': '.sh'}

    @staticmethod
    def _read_sync_state(directory):
        """Bidirectional file sync -- the {name: {hash, synced_at}}
        baseline sidecar for a synced-entries folder (custom_aiml/
        custom_maps; custom_node_files tracks the same fields inline on
        its own manifest.json instead, since it already has one)."""
        path = os.path.join(directory, 'sync_state.json')
        if not os.path.exists(path):
            return {}
        with open(path, 'r') as file:
            try:
                return json.load(file)
            except json.JSONDecodeError:
                return {}

    @staticmethod
    def _write_sync_state(directory, state):
        with open(os.path.join(directory, 'sync_state.json'), 'w') as file:
            json.dump(state, file)

    def sync_custom_node_files(self, custom_node_files=None):
        """Multi-language custom BT node system, Phase 5/6/7 -- the robot-
        side half of apps/analytics/models.py's CustomFile/
        CustomNodeDefinition, now covering all four languages. `custom_node_files`
        is {file_name: {"language", "source", "xml_tag", "node_type",
        "header_source", "dependencies", "ports"}} for every project
        CustomFile that's currently exposed as a node (DataAnalysis's own
        sync helper only ever sends that subset -- a plain source file
        with no CustomNodeDefinition never reaches the robot at all, it
        has nothing to register), fresh from Django when resyncing.

        Deliberately its own folder/manifest (custom_behaviors/
        custom_node_files/, own _custom_node_file_tags tracking set) --
        NOT the same as sync_bt_inline_nodes' inline_nodes/ folder (a
        different Django model/dispatch key/trust model: CustomFile is
        real per-project source-controlled code with its own file-name
        identity, not admin-authored inline nodes keyed by node name) and
        NOT xparo_custom_files_folder_path (confirmed by inspection to be
        an unrelated, pre-existing "Sets"/data-file sync with nothing to
        do with code -- reusing it would silently collide two unrelated
        features).

        Bidirectional file sync (see /home/scientist/.claude/plans/
        breezy-splashing-koala.md): Django-synced source files live one
        level deeper than before, under a subfolder per language
        (custom_node_files/{python,cpp,javascript,bash}/) -- a real,
        git-trackable, language-separated layout instead of one flat
        folder. A second, git-tracked `examples_manifest.json` sits
        alongside the real `manifest.json` and is NEVER written by this
        method -- it ships two known-good example nodes (one Action, one
        Condition) per language, registered at startup with zero Django
        connection required. The effective registry is
        `examples_manifest.json UNION manifest.json`; a real Django-synced
        entry wins on a name collision (logged as a sync_failures entry
        for the shadowed example, not silently dropped).

        Registration differs by language (see runners.py's own module
        docstring for why): Python self-describes its own tag via
        `XML_TAG = "..."`, scanned by plugin_loader's existing class
        introspection -- exactly like sync_bt_inline_nodes/sync_bt_plugins
        already do. JavaScript/Bash/C++ have no importable-and-scannable
        source the same way, so their xml_tag/ports/node_type ride along
        in the manifest itself (persisted here, not re-derived) and get
        registered directly into NODE_REGISTRY. A C++ entry that fails to
        compile is skipped (logged, not raised) -- the rest of the sync,
        every other language and every other file, still proceeds; this
        mirrors plugin_loader.load_plugins' own "one bad file contributes
        nothing, never blocks the rest" posture. An unrecognized language
        is skipped just as quietly.

        None means "load whatever was persisted from a previous sync"
        (the startup case, called once from xparo_ros.py's __init__,
        mirroring sync_bt_plugins/sync_bt_inline_nodes) -- the manifest
        carries everything needed to re-register without Django resending
        anything, since the actual source files are already on disk.

        Returns a list of {"name", "language", "reason"} for every entry
        that was skipped rather than registered -- these used to only ever
        be printed here (never reaching Django/the dashboard at all,
        i.e. a project owner editing this file had no way to know a
        compile failed short of watching the robot's own stdout). The
        caller (on_ws_message's "custom_node_files" branch) turns this
        into a CUSTOM_NODE_SYNC_RESULT ack.
        """
        from .bt_engine.node_registry import NODE_REGISTRY

        sync_failures = []

        folder = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_node_files')
        manifest_path = os.path.join(folder, 'manifest.json')
        examples_manifest_path = os.path.join(folder, 'examples_manifest.json')

        def _language_dir(language):
            return os.path.join(folder, language)

        if custom_node_files is not None:
            os.makedirs(folder, exist_ok=True)
            previous_manifest = {}
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r') as file:
                    try:
                        previous_manifest = json.load(file)
                    except json.JSONDecodeError:
                        previous_manifest = {}

            manifest = {}
            for name, entry in custom_node_files.items():
                if not isinstance(entry, dict):
                    continue
                language = entry.get('language')
                extension = self._CUSTOM_NODE_FILE_EXTENSIONS.get(language)
                if extension is None:
                    continue  # unrecognized language -- not written, not registered
                language_dir = _language_dir(language)
                os.makedirs(language_dir, exist_ok=True)
                source_path = os.path.join(language_dir, name + extension)
                with open(source_path, 'w') as file:
                    file.write(entry.get('source', ''))
                if language == 'bash':
                    os.chmod(source_path, 0o755)
                # A fresh Django sync is by definition the new authoritative
                # baseline -- content_hash/synced_at get set unconditionally
                # here (see /home/scientist/.claude/plans/breezy-splashing-
                # koala.md's Part 2/3: "Django changed -> push to robot,
                # update baseline" applies to every entry Django just sent).
                manifest[name] = {
                    'language': language,
                    'xml_tag': entry.get('xml_tag', ''),
                    'node_type': entry.get('node_type', 'action'),
                    'ports': entry.get('ports', []),
                    'header_source': entry.get('header_source', ''),
                    'content_hash': sync_hash.content_hash(entry.get('source', ''), entry.get('header_source', '')),
                    'synced_at': datetime.utcnow().isoformat(),
                }

            for stale_name, stale_entry in previous_manifest.items():
                if stale_name in manifest:
                    continue
                stale_language = stale_entry.get('language')
                stale_extension = self._CUSTOM_NODE_FILE_EXTENSIONS.get(stale_language)
                if stale_extension is None:
                    continue  # unrecognized/missing language -- nothing to clean up
                for suffix in (stale_extension, '.hpp'):
                    try:
                        os.remove(os.path.join(_language_dir(stale_language), stale_name + suffix))
                    except OSError:
                        pass

            with open(manifest_path, 'w') as file:
                json.dump(manifest, file)

        manifest = {}
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as file:
                try:
                    manifest = json.load(file)
                except json.JSONDecodeError:
                    manifest = {}

        # Bootstrap-on-read (see the plan's Part 5): a manifest entry
        # written before this hash-tracking existed has no content_hash at
        # all. Rather than a one-time migration (robots don't run Django
        # migrations), compute it from whatever's on disk RIGHT NOW the
        # first time it's read and write it back -- that becomes the new
        # baseline, never treated as a retroactive conflict just because
        # tracking is new.
        manifest_needs_rewrite = False
        for name, entry in manifest.items():
            if entry.get('content_hash'):
                continue
            extension = self._CUSTOM_NODE_FILE_EXTENSIONS.get(entry.get('language'))
            if extension is None:
                continue
            source_path = os.path.join(_language_dir(entry['language']), name + extension)
            try:
                with open(source_path, 'r') as file:
                    source_on_disk = file.read()
            except OSError:
                continue
            entry['content_hash'] = sync_hash.content_hash(source_on_disk, entry.get('header_source', ''))
            entry['synced_at'] = datetime.utcnow().isoformat()
            manifest_needs_rewrite = True
        if manifest_needs_rewrite:
            with open(manifest_path, 'w') as file:
                json.dump(manifest, file)

        # Git-tracked, sync-code-read-only -- ships two known-good example
        # nodes (one Action, one Condition) per language, usable with zero
        # Django connection ever established (a fresh `colcon build
        # --symlink-install` alone is enough). Nested by language (unlike
        # the real manifest.json's flat shape) so the same simple file
        # name (e.g. greet_example) can exist once per language folder
        # without colliding as JSON object keys -- each language's own
        # xml_tag is still unique project-wide, matching CustomFile's own
        # real constraint. A real Django-synced entry always wins on a
        # name collision within the same language -- and the ordinary,
        # expected way that happens now is get_local_file_state/
        # _discover_new_node_files adopting an example into Django once
        # it's seen on disk, at which point manifest.json's entry IS
        # this example, byte-for-byte, forever after. Only a name
        # collision where the content has genuinely diverged (a real
        # coincidence, not an adoption) is worth flagging as a
        # sync_failures entry -- checked by content_hash, not just name,
        # so an adopted example never spams a false "shadowed" report on
        # every subsequent Django push.
        examples_manifest = {}
        if os.path.exists(examples_manifest_path):
            with open(examples_manifest_path, 'r') as file:
                try:
                    examples_manifest = json.load(file)
                except json.JSONDecodeError:
                    examples_manifest = {}

        effective_manifest = dict(manifest)
        for language, language_examples in examples_manifest.items():
            if not isinstance(language_examples, dict):
                continue
            example_extension = self._CUSTOM_NODE_FILE_EXTENSIONS.get(language)
            for name, entry in language_examples.items():
                if name in effective_manifest:
                    example_hash = None
                    if example_extension:
                        try:
                            with open(os.path.join(_language_dir(language), name + example_extension), 'r') as file:
                                example_hash = sync_hash.content_hash(file.read(), entry.get('header_source', ''))
                        except OSError:
                            pass
                    if example_hash != effective_manifest[name].get('content_hash'):
                        sync_failures.append({
                            "name": name, "language": language,
                            "reason": "shadowed by a real custom node file with the same name",
                        })
                    continue
                effective_manifest[name] = {**entry, 'language': language}
        manifest = effective_manifest

        # Registrations are built first and swapped in at the end; only
        # tags that really went away are removed. This used to unregister
        # EVERY custom node first and re-register them one by one --
        # recompiling each C++ node in between, which takes seconds -- so
        # a task arriving meanwhile (Run now right after the robot came
        # online, an auto-launch task, anything after a reconnect) failed
        # with "<greet_example_cpp> isn't a node this robot knows".
        # Confirmed live on a real robot.
        previous_tags = set(self._custom_node_file_tags)
        pending = {}
        registered_tags = set()

        python_paths = [
            os.path.join(_language_dir('python'), name + '.py')
            for name, entry in manifest.items() if entry.get('language') == 'python'
        ]
        registered_tags |= set(plugin_loader.register_plugins(python_paths).keys())

        js_entries = [(name, entry) for name, entry in manifest.items() if entry.get('language') == 'javascript']
        if js_entries:
            js_dir = _language_dir('javascript')
            js_runtime_dir = os.path.join(js_dir, 'js_runtime')
            runners.ensure_js_runtime(js_runtime_dir)
            for name, entry in js_entries:
                xml_tag = entry.get('xml_tag')
                if not xml_tag:
                    sync_failures.append({"name": name, "language": "javascript", "reason": "no xml_tag configured"})
                    continue
                output_keys = [p['key'] for p in entry.get('ports', []) if p.get('direction') == 'output']
                pending[xml_tag] = runners.make_javascript_node_factory(
                    os.path.join(js_dir, name + '.js'), js_runtime_dir, output_keys,
                )
                registered_tags.add(xml_tag)

        bash_dir = _language_dir('bash')
        for name, entry in manifest.items():
            if entry.get('language') != 'bash':
                continue
            xml_tag = entry.get('xml_tag')
            if not xml_tag:
                sync_failures.append({"name": name, "language": "bash", "reason": "no xml_tag configured"})
                continue
            script_path = os.path.join(bash_dir, name + '.sh')

            def _bash_builder(nm, attrs, blackboard, children, ros_node, script_path=script_path):
                return runners.BashProcessNode(nm, attrs, blackboard, script_path=script_path, ros_node=ros_node)

            pending[xml_tag] = _bash_builder
            registered_tags.add(xml_tag)

        cpp_entries = [(name, entry) for name, entry in manifest.items() if entry.get('language') == 'cpp']
        if cpp_entries:
            cpp_dir = _language_dir('cpp')
            cpp_build_dir = os.path.join(cpp_dir, 'cpp_build')
            for name, entry in cpp_entries:
                xml_tag = entry.get('xml_tag')
                cpp_path = os.path.join(cpp_dir, name + '.cpp')
                if not xml_tag:
                    sync_failures.append({"name": name, "language": "cpp", "reason": "no xml_tag configured"})
                    continue
                if not os.path.exists(cpp_path):
                    sync_failures.append({"name": name, "language": "cpp", "reason": "source file missing on disk"})
                    continue
                with open(cpp_path, 'r') as file:
                    source = file.read()
                output_keys = [p['key'] for p in entry.get('ports', []) if p.get('direction') == 'output']
                executable_path, reason = runners.compile_cpp_node(source, entry.get('header_source', ''), cpp_build_dir, name)
                if executable_path is None:
                    sync_failures.append({"name": name, "language": "cpp", "reason": reason or "compile failed"})
                    continue
                pending[xml_tag] = runners.make_cpp_node_factory(executable_path, output_keys)
                registered_tags.add(xml_tag)

        NODE_REGISTRY.update(pending)
        plugin_loader.unregister_tags(previous_tags - registered_tags)
        self._custom_node_file_tags = registered_tags
        print(f"[bt_engine] loaded custom node file tags: {sorted(registered_tags)}")
        return sync_failures

    def sync_rosbag_config(self, config):
        """`config` is apps/analytics/data_analyis.py's
        GET_rosbag_config/EDIT_rosbag_config shape ({record_all,
        ignore_topics, include_topics, start_mode, start_delay_seconds}),
        always real data -- unlike sync_bt_plugins/sync_bt_inline_nodes,
        this has no "load whatever was persisted, on startup" mode of its
        own, since xparo_ros.py already reads the persisted file directly
        (rosbag_control.load_rosbag_config) *before* RosbagControl is
        constructed -- its __init__ kicks off the boot sequence
        immediately, so it can't wait for Engine to exist and sync
        afterward the way BT plugin config does.

        Persists to custom_behaviors/rosbag_config.json (the same file
        load_rosbag_config reads) and, if this robot is actually
        recording, applies start_mode/start_delay_seconds to the live
        RosbagControl immediately. record_all/ignore_topics/
        include_topics only take effect on this robot's *next* launch --
        see rosbag_control.py's own module comment on why topic selection
        can't be re-applied to an already-running recorder process.
        """
        # Local import (not at module top like bt_engine's plugin_loader/
        # run_task) -- rosbag_control.py has a real rclpy/rosbag2_interfaces
        # dependency, and engine.py is deliberately usable standalone
        # outside a ROS2 context (see its own __main__ block and the test
        # suite's connection_type="offline" construction); only pay that
        # import cost when this method is actually called.
        from .rosbag_control import ROSBAG_CONFIG_FILENAME
        pth = os.path.join(self.files["xparo_custom_behaviors_folder_path"], ROSBAG_CONFIG_FILENAME)
        os.makedirs(os.path.dirname(pth), exist_ok=True)
        with open(pth, 'w') as file:
            json.dump(config, file)

        # bt_executor.node is the live Xparo node -- its own
        # rosbag_control (if this robot was launched with
        # record_bags:=true) is the thing start_mode/start_delay_seconds
        # actually apply to. None if this robot isn't recording bags at
        # all, same "safe no-op" posture RUN_TASK's own
        # bt_executor-is-None check has.
        if self.bt_executor is not None and getattr(self.bt_executor, 'node', None) is not None:
            live_control = getattr(self.bt_executor.node, 'rosbag_control', None)
            if live_control is not None:
                live_control.start_mode = config.get('start_mode', live_control.start_mode)
                live_control.start_delay_seconds = max(0, config.get('start_delay_seconds', live_control.start_delay_seconds) or 0)
        print(f"[rosbag_control] synced config: {config}")

    def sync_custom_tasks(self, tasks):
        """`tasks` is apps/analytics/data_analyis.py's
        DataAnalysis._get_custom_tasks shape ({task_id: {behaviour_tree_name,
        blackboard_mapping, params, save_task_history}}) -- persisted to
        custom_behaviors/tasks.json (same folder/pattern rosbag_config.json
        already uses) so a task can be triggered locally, without a Django
        round trip, by publishing its task_id on /xparo/run_task (see
        run_task_from_topic below and xparo_ros.py's subscription to that
        topic). Sent on every connect (get_init_api_client_data) and
        pushed live on every task add/edit/delete/copy (Manage_Dash.py),
        matching rosbag_config's own sync cadence.
        """
        from .bt_engine.task_sync import TASKS_FILENAME
        pth = os.path.join(self.files["xparo_custom_behaviors_folder_path"], TASKS_FILENAME)
        os.makedirs(os.path.dirname(pth), exist_ok=True)
        with open(pth, 'w') as file:
            json.dump(tasks, file)
        print(f"[task_sync] synced {len(tasks)} task(s)")

    def run_task_from_topic(self, task_id, override_params=None):
        """/xparo/run_task's handler (xparo_ros.py) -- the locally-
        triggered counterpart to on_ws_message's own "RUN_TASK" branch
        just below, sharing the exact same dispatch (run_task.
        handle_run_task, one thread per task, matching RUN_COMMAND's
        established pattern) but resolving tree_xml/blackboard from this
        robot's own already-synced local files (bt_engine.task_sync)
        instead of receiving them pre-resolved from Django. TASK_RESULT
        and (if the task has save_task_history on) its history row still
        get reported back over whatever transport is configured --
        handle_run_task doesn't know or care which path triggered it, so
        RUN_TASK's own single-run-deletion/restart-on-failure logic
        (Django's TASK_RESULT handler) keeps working unchanged too.
        """
        from .bt_engine import task_sync
        if self.bt_executor is None:
            print("[run_task_from_topic] no live bt_executor -- ignoring")
            self._task_finished(run_task.result_message(task_id, None, "no_executor", NO_EXECUTOR_ERROR))
            return
        custom_tasks = task_sync.load_custom_tasks(self.files["xparo_custom_behaviors_folder_path"])
        val = task_sync.build_run_task_val(task_id, override_params, custom_tasks, self.files)
        if val is None:
            print(f"[run_task_from_topic] task_id {task_id!r} not in the local sync cache -- "
                  f"either it doesn't exist, or this robot hasn't synced since it was created")
            self._task_finished(run_task.result_message(
                task_id, None, "unknown_task",
                f"Task {task_id!r} isn't in this robot's synced task list -- it doesn't exist, or the robot "
                f"hasn't connected to the dashboard since the task was created.",
                trigger="ros_topic",
            ))
            return
        self._start_task_thread(val)

    def _start_task_thread(self, val):
        """One thread per task run (RUN_COMMAND's pattern). SubTrees the
        dispatch didn't include are read from this robot's synced trees.

        send_response (-> TASK_RESULT) goes through _send_important_dict,
        not the plain _send_dict on_started uses -- a task's FINAL result
        must survive a connection outage that happens to line up with the
        moment it finishes (2026-09-28 stress test finding F5, confirmed
        live); "it started" is a live nicety only, not worth resurrecting
        and delivering late/out of order after a long reconnect gap.
        """
        from .bt_engine import task_sync
        threading.Thread(
            target=run_task.handle_run_task,
            args=(self.bt_executor, val, self._task_finished),
            kwargs={
                "add_task_history": self.add_task_history,
                "xparo_stage": self.xparo_stage,
                "subtree_resolver": lambda name: task_sync.resolve_tree_xml(name, self.files),
                "on_started": self._task_started,
            },
            daemon=True,
        ).start()

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
                # Bidirectional file sync: a synced custom_aiml entry used
                # to be written directly into xparo_custom_behaviors_folder_path's
                # own top level -- the exact same folder curated fixtures
                # like quick_delivery_tree.xml live in. A user's tree
                # merely sharing that name silently overwrote the fixture
                # (a real, observed data-loss bug -- confirmed via `git
                # status` showing quick_delivery_tree.xml dirty from
                # exactly this). Its own subfolder makes that collision
                # structurally impossible, and keeps the top level a
                # curated-examples-only zone permanently.
                custom_aiml_dir = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_aiml')
                os.makedirs(custom_aiml_dir, exist_ok=True)
                aiml_sync_state = self._read_sync_state(custom_aiml_dir)
                for kk,vv in val.items():
                    # kk is a tree name a project member typed on the
                    # dashboard -- Django validates it (apps/analytics/
                    # models.py's validate_custom_file_name) before ever
                    # saving/relaying it, but this robot doesn't trust that
                    # alone: confirmed live that without this check, a name
                    # like "../../etc/whatever" writes straight through to
                    # that traversed path, past the package root, with
                    # fully attacker-controlled content. Skip (not abort
                    # the whole sync) so the rest of a legitimate batch
                    # still lands.
                    safe_pth = remote_ops.safe_path_in(custom_aiml_dir, kk + '.xml')
                    if safe_pth is None:
                        print(f"[custom_aiml sync] refusing unsafe tree name {kk!r} (path traversal attempt)")
                        continue
                    pth = str(safe_pth)
                    content =  f'''<root BTCPP_format="4" main_tree_to_execute="MainTree">
<BehaviorTree ID="MainTree">
{vv}
</BehaviorTree>
</root>'''
                    self.local_database.load_or_create_file(pth,content)
                    with open(pth, 'w') as file:
                        file.write(content)
                    # Fresh Django content is the new authoritative
                    # baseline -- same "Django changed -> update baseline"
                    # outcome the custom_node_files manifest already
                    # applies, hashing the raw tree text (vv), not the
                    # <root>/<BehaviorTree> wrapper around it.
                    aiml_sync_state[kk] = {
                        "hash": sync_hash.content_hash(vv),
                        "synced_at": datetime.utcnow().isoformat(),
                    }
                self._write_sync_state(custom_aiml_dir, aiml_sync_state)
            elif k=="custom_maps":
                # Same fix, same reasoning as custom_aiml just above --
                # its own subfolder under xparo_custom_evns_folder_path,
                # leaving that root free for any future curated env
                # examples the same way.
                custom_maps_dir = os.path.join(self.files["xparo_custom_evns_folder_path"], 'custom_maps')
                os.makedirs(custom_maps_dir, exist_ok=True)
                maps_sync_state = self._read_sync_state(custom_maps_dir)
                for kk,vv in val.items():
                    # Same guard as custom_aiml just above, same confirmed
                    # bug it closes -- see that block's comment.
                    safe_pth = remote_ops.safe_path_in(custom_maps_dir, kk + '.env')
                    if safe_pth is None:
                        print(f"[custom_maps sync] refusing unsafe env name {kk!r} (path traversal attempt)")
                        continue
                    pth = str(safe_pth)
                    content =  f'''{vv}'''
                    self.local_database.load_or_create_file(pth,content)
                    with open(pth, 'w') as file:
                        file.write(content)
                    maps_sync_state[kk] = {
                        "hash": sync_hash.content_hash(vv),
                        "synced_at": datetime.utcnow().isoformat(),
                    }
                self._write_sync_state(custom_maps_dir, maps_sync_state)
            elif k=="custom_Sets" or k=="custom_sets":
                for kk,vv in val.items():
                    # Unlike custom_aiml/custom_maps just above, kk here
                    # has no forced extension at all -- full filename
                    # control -- so the same guard matters even more.
                    safe_pth = remote_ops.safe_path_in(self.files["xparo_custom_files_folder_path"], kk)
                    if safe_pth is None:
                        print(f"[custom_Sets sync] refusing unsafe file name {kk!r} (path traversal attempt)")
                        continue
                    pth = str(safe_pth)
                    content =  f'''{vv}'''
                    self.local_database.load_or_create_file(pth,content)
                    with open(pth, 'w') as file:
                        file.write(content)
            elif k=="custom_bt_node_plugins":
                # Phase 12 -- val is Project_Dashboard.custom_bt_node_plugins's
                # shape ([{"path":..., "enabled":...}]), same "persist to
                # disk, mirroring custom_aiml's pattern" as everything else
                # in this block, then actually (re)load whichever paths are
                # enabled into NODE_REGISTRY.
                self.sync_bt_plugins(val)
            elif k=="custom_bt_node_inline_code":
                # Phase 13 -- val is Project_Dashboard.custom_bt_node_inline_code's
                # shape ({node_name: python_source}). Django only ever
                # sends this once role-gating + the allow_inline_bt_code
                # opt-in have both already passed (Manage_Dash.py's
                # save_aiml), so no trust decision is made here -- this
                # robot trusts whatever its own project's Django instance
                # relays, same as every other sync branch in this method.
                self.sync_bt_inline_nodes(val)
            elif k=="rosbag_config":
                # val is DataAnalysis._get_rosbag_config()'s shape
                # ({record_all, ignore_topics, include_topics, start_mode,
                # start_delay_seconds}) -- see sync_rosbag_config's own
                # docstring for what does/doesn't apply live vs. on next
                # launch.
                self.sync_rosbag_config(val)
            elif k=="ads_schedule":
                # Ads Center (apps/ads): the ads this robot's owner approved
                # -- the reply to GET_ads_schedule, or pushed on any change.
                if self.ad_manager is not None:
                    self.ad_manager.update_schedule(val)
            elif k=="ads_plays_ack":
                # Which uploaded plays (ADS_PLAYS) the server stored.
                if self.ad_manager is not None:
                    self.ad_manager.on_ack(val)
            elif k=="custom_tasks":
                # val is DataAnalysis._get_custom_tasks()'s shape
                # ({task_id: {behaviour_tree_name, blackboard_mapping,
                # params, save_task_history}}) -- see sync_custom_tasks'
                # own docstring.
                self.sync_custom_tasks(val)
            elif k=="custom_node_files":
                # Multi-language custom BT node system, Phase 5 -- val is
                # DataAnalysis._get_custom_node_files_for_sync()'s shape
                # ({file_name: {"language":..., "source":...}}). See
                # sync_custom_node_files' own docstring for what happens
                # to non-Python entries today.
                #
                # Always ack, matching FILE_LIST/DELETE_ACK/TELEOP_ACK's
                # own "the caller always hears back" convention -- a
                # compile/registration failure used to only ever be
                # printed here, invisible to whoever just edited the file
                # from the dashboard.
                sync_failures = self.sync_custom_node_files(val)
                self._send_dict({"CUSTOM_NODE_SYNC_RESULT": {
                    "registered_tags": sorted(self._custom_node_file_tags),
                    "failures": sync_failures,
                }})
            elif k=="REQUEST_LOCAL_FILE_CONTENT":
                # Bidirectional file sync (see /home/scientist/.claude/
                # plans/breezy-splashing-koala.md, Part 5): Django only
                # ever asks for names whose hash it couldn't already
                # resolve as a clean push-down -- this always answers with
                # actual content, never decides anything about conflicts
                # itself (that's entirely Django's call, see
                # DataAnalysis.resolve_local_file_content); if Django ends
                # up not pushing anything back for a name because it
                # turned out to be a real conflict, this robot's own local
                # copy is simply left exactly as it was.
                self._send_dict({"LOCAL_FILE_CONTENT": self.get_local_file_content(val)})
            elif k=="ROBOT_CREDENTIAL":
                self._persist_credential(val)
            elif k=="get_initial_local_env_data":
                self.get_initial_local_env_data()
            elif k=="sync_local_database":
                # Bidirectional file sync -- see get_local_file_state's own
                # docstring for why this is a real, hash-aware report now
                # instead of the unconditional raw-content push this used
                # to be.
                self._send_dict({"LOCAL_FILE_STATE": self.get_local_file_state()})
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
                max_lines = val.get("max_lines")
                if command.strip():
                    threading.Thread(
                        target=remote_ops.handle_run_command,
                        args=(command, request_id, timeout, self._send_dict),
                        kwargs={"max_lines": max_lines},
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
                # send_response lets a size-limit violation (finding F7)
                # actually reach the sender instead of failing silently.
                self.file_transfer.handle_file_chunk(val, self._send_dict)
            elif k=="FILE_COMPLETE":
                self.file_transfer.handle_file_complete(self._send_dict)
            # ---- Fleet-management popups ported from the AUV GCS (see
            # /home/scientist/.claude/plans/breezy-splashing-koala.md) --
            # same remote_ops.py "plain function + send_response callback"
            # shape as the Phase 4 remote-ops handlers just above.
            elif k=="REBOOT_ROBOT":
                threading.Thread(
                    target=remote_ops.handle_reboot,
                    args=(val.get("password"), self._send_dict),
                    daemon=True,
                ).start()
            elif k=="GET_ROS2_TOPICS":
                if self.bt_executor is not None:
                    remote_ops.handle_list_ros2_topics(self.bt_executor.node, self._send_dict)
                else:
                    self._send_dict({"ROS2_TOPICS": {"topics": []}})
            elif k=="GET_ROS2_PARAMS":
                if self.bt_executor is not None:
                    threading.Thread(
                        target=remote_ops.handle_list_ros2_params,
                        args=(self.bt_executor.node, self._send_dict),
                        daemon=True,
                    ).start()
                else:
                    self._send_dict({"ROS2_PARAMS": {"params": [], "errors": ["no live ROS2 node on this connection"]}})
            elif k=="SET_ROS2_PARAM":
                threading.Thread(
                    target=remote_ops.handle_set_ros2_param,
                    args=(val.get("node", ""), val.get("name", ""), val.get("value", ""), val.get("request_id"), self._send_dict),
                    daemon=True,
                ).start()
            elif k=="GET_ROSBAG_STATUS":
                self._send_dict({"ROSBAG_STATUS": remote_ops.get_rosbag_status(self._get_rosbag_control())})
            elif k in ("START_ROSBAG", "STOP_ROSBAG", "SAVE_ROSBAG"):
                action = {"START_ROSBAG": "start", "STOP_ROSBAG": "stop", "SAVE_ROSBAG": "save"}[k]
                remote_ops.handle_rosbag_action(self._get_rosbag_control(), action, self._send_dict)
            elif k=="WATCH_ERROR_LOGS":
                # Health & Errors popup is open with "Watch live" on: push
                # the component board every flush (see flush_health). Ends
                # on UNWATCH, or by itself after HEALTH_WATCH_SEC in case
                # the browser went away without saying so.
                self._health_watch_until = time.monotonic() + HEALTH_WATCH_SEC
                self._refresh_self_diagnostics()
                aggregator = self._get_diagnostics_aggregator()
                if aggregator is not None:
                    self._send_dict({"DIAGNOSTICS_SNAPSHOT": aggregator.snapshot()})
                self.flush_health()
            elif k=="UNWATCH_ERROR_LOGS":
                self._health_watch_until = 0.0
            elif k=="GET_LIVE_STATUS":
                threading.Thread(
                    target=remote_ops.handle_get_live_status,
                    args=(
                        self.local_database.get_smart_resource_consumption,
                        self.local_database.get_cpu_temperature,
                        time.time() - self._engine_started_at,
                        self._send_dict,
                    ),
                    daemon=True,
                ).start()
            # ---- Wi-Fi / Bluetooth popups -- see connectivity.py. Each
            # runs in its own thread: a rescan, a Bluetooth scan or a
            # network switch all block for seconds.
            elif k=="GET_WIFI_NETWORKS":
                threading.Thread(
                    target=connectivity.handle_get_wifi_networks,
                    args=(bool((val or {}).get("rescan")), self._send_dict),
                    daemon=True,
                ).start()
            elif k=="WIFI_CONNECT":
                # The result is sent with _send_important_dict: switching
                # networks can drop this very connection, and the result
                # must still arrive once the robot is back.
                threading.Thread(
                    target=connectivity.handle_wifi_connect,
                    args=(val, self._send_important_dict),
                    kwargs={
                        "is_server_reachable": self._xparo_server_reachable,
                        "on_network_changed": self._on_network_changed,
                    },
                    daemon=True,
                ).start()
            elif k=="WIFI_FORGET":
                threading.Thread(target=connectivity.handle_wifi_forget, args=(val, self._send_dict), daemon=True).start()
            elif k=="WIFI_RADIO":
                threading.Thread(target=connectivity.handle_wifi_radio, args=(val, self._send_dict), daemon=True).start()
            elif k=="GET_BLUETOOTH_DEVICES":
                threading.Thread(
                    target=connectivity.handle_get_bluetooth_devices,
                    args=(bool((val or {}).get("scan")), self._send_dict),
                    daemon=True,
                ).start()
            elif k=="BLUETOOTH_ACTION":
                threading.Thread(target=connectivity.handle_bluetooth_action, args=(val, self._send_dict), daemon=True).start()
            elif k=="GET_DIAGNOSTICS_SNAPSHOT":
                self._refresh_self_diagnostics()
                aggregator = self._get_diagnostics_aggregator()
                snapshot = aggregator.snapshot() if aggregator is not None else {"components": {}, "overall_level": None}
                self._send_dict({"DIAGNOSTICS_SNAPSHOT": snapshot})
            elif k=="GET_XPARO_VERSION":
                self._send_dict({"XPARO_VERSION": {
                    "xparo_git_commit": self.local_database.get_xparo_git_commit(),
                    "ros_distro": os.environ.get("ROS_DISTRO"),
                }})
            elif k=="RUN_TASK":
                # bt_executor is None when this Engine isn't owned by a
                # live Xparo node (standalone use, or most tests in this
                # repo). It used to drop the task silently, leaving the
                # dashboard waiting; now it says why, and marks it as not
                # worth retrying (restart_on_failure would just loop).
                if self.bt_executor is not None:
                    self._start_task_thread(val)
                else:
                    self._task_finished(run_task.result_message(
                        (val or {}).get("task_id"), (val or {}).get("run_id"), "no_executor", NO_EXECUTOR_ERROR,
                    ))
            elif k=="CANCEL_TASK":
                val = val or {}
                cancelled = run_task.cancel_task(
                    task_id=val.get("task_id"), run_id=val.get("run_id"),
                    reason=val.get("reason") or "cancelled from the dashboard",
                )
                self._send_dict({"TASK_CANCEL_ACK": {
                    "task_id": val.get("task_id"), "run_ids": cancelled, "found": bool(cancelled),
                    "message": ("Stopping..." if cancelled else "That task isn't running on this robot."),
                }})
            elif k=="GET_RUNNING_TASKS":
                self._send_dict({"RUNNING_TASKS": {"runs": run_task.active_runs()}})
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

        # ---- Read custom_aiml (all .xml files in custom_behaviors/custom_aiml) ----
        custom_behaviors_path = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_aiml')
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

        # ---- Read custom_maps (all .env files in custom_envs/custom_maps) ----
        custom_envs_path = os.path.join(self.files["xparo_custom_evns_folder_path"], 'custom_maps')
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

        return result

    # Bidirectional file sync, "adopt a disk-added file" (see /home/
    # scientist/.claude/plans/breezy-splashing-koala.md): a file the owner
    # drops directly into a language folder -- not via the dashboard, not
    # in manifest.json/examples_manifest.json at all -- needs to be
    # DISCOVERED before it can even be reported to Django. Only files that
    # actually look like a real node's entry point count; a shared helper/
    # include file (a plain utility module with no node class, a .hpp
    # header already handled separately as header_source) is deliberately
    # never surfaced here, matching the owner's own explicit "no need to
    # sync its importable/include files, just the main file" instruction.
    # C++'s check mirrors runners.py's own _CPP_CLASS_RE exactly (cheap
    # structural match -- the real compile attempt only happens later,
    # once Django has actually ingested this as a CustomFile).
    _JS_NODE_CLASS_RE = re.compile(r"class\s+\w+\s+extends\s+XparoNode")

    def _looks_like_a_node_file(self, language, source):
        if language == 'python':
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
                tmp.write(source)
                tmp_path = tmp.name
            try:
                return bool(plugin_loader.load_plugins([tmp_path]))
            except Exception:
                return False
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        if language == 'cpp':
            return bool(runners._CPP_CLASS_RE.search(source))
        if language == 'javascript':
            return bool(self._JS_NODE_CLASS_RE.search(source))
        if language == 'bash':
            # No import/include convention exists for Bash at all -- every
            # .sh file in this folder is inherently "a main file".
            return True
        return False

    def _discover_new_node_files(self, known_names):
        """Scans every language subfolder for files not already tracked
        (in manifest.json or examples_manifest.json) that look like a
        real node's entry point. Returns {name: {language, source,
        header_source}} -- header_source stays '' here (a bare .hpp with
        no matching .cpp of the same name is exactly the "include file"
        case this deliberately skips)."""
        base = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_node_files')
        discovered = {}
        for language, extension in self._CUSTOM_NODE_FILE_EXTENSIONS.items():
            language_dir = os.path.join(base, language)
            if not os.path.isdir(language_dir):
                continue
            for filename in os.listdir(language_dir):
                if not filename.endswith(extension):
                    continue
                name = filename[:-len(extension)]
                if name in known_names:
                    continue
                source_path = os.path.join(language_dir, filename)
                try:
                    with open(source_path, 'r') as file:
                        source = file.read()
                except OSError:
                    continue
                if not self._looks_like_a_node_file(language, source):
                    continue
                header_source = ''
                if language == 'cpp':
                    header_path = os.path.join(language_dir, name + '.hpp')
                    if os.path.exists(header_path):
                        with open(header_path, 'r') as file:
                            header_source = file.read()
                discovered[name] = {"language": language, "source": source, "header_source": header_source}
        return discovered

    _CPP_CLASS_AND_BASE_RE = re.compile(r"class\s+(\w+)\s*:\s*public\s+BT::(SyncActionNode|ConditionNode)")
    _CPP_PORT_RE = re.compile(r'BT::(InputPort|OutputPort)\s*<[^>]*>\s*\(\s*"([^"]+)"')
    _JS_PORT_RE = re.compile(r'this\.(input|output)\s*\(\s*"([^"]+)"')
    _BASH_INPUT_RE = re.compile(r'\$\{(\w+):-')
    _BASH_OUTPUT_RE = re.compile(r'echo\s+"(\w+)=')

    @staticmethod
    def _pascal_case(name):
        """Duplicated (not imported -- separate git repo, no shared
        import path) from apps/analytics/custom_node_files.py's own
        _pascal_case, kept in lockstep by convention the same way
        sync_hash.py's two copies are."""
        parts = [p for p in name.replace('-', '_').split('_') if p]
        return ''.join(p[0].upper() + p[1:] for p in parts) or 'CustomNode'

    def _detect_node_metadata(self, name, language, source):
        """Best-effort {xml_tag, node_type, ports} for a file Django has
        never seen before -- used only to bootstrap a brand-new
        CustomNodeDefinition on first ingest (see
        DataAnalysis._ingest_disk_content on the Django side); once one
        exists, this is never consulted again -- Django's own value is
        always authoritative from then on, same as every other synced
        entry's metadata.
        """
        if language == 'python':
            import tempfile
            xml_tag = self._pascal_case(name)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
                tmp.write(source)
                tmp_path = tmp.name
            try:
                found = plugin_loader.load_plugins([tmp_path])
                if found:
                    xml_tag = next(iter(found.keys()))
            except Exception:
                pass
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return {"xml_tag": xml_tag, "node_type": "action", "ports": []}

        if language == 'cpp':
            match = self._CPP_CLASS_AND_BASE_RE.search(source)
            xml_tag = match.group(1) if match else self._pascal_case(name)
            node_type = "condition" if match and match.group(2) == "ConditionNode" else "action"
            ports = [
                {"direction": "input" if kind == "InputPort" else "output", "key": key}
                for kind, key in self._CPP_PORT_RE.findall(source)
            ]
            return {"xml_tag": xml_tag, "node_type": node_type, "ports": ports}

        if language == 'javascript':
            class_match = re.search(r"class\s+(\w+)\s+extends\s+XparoNode", source)
            xml_tag = class_match.group(1) if class_match else self._pascal_case(name)
            ports = [
                {"direction": kind, "key": key}
                for kind, key in self._JS_PORT_RE.findall(source)
            ]
            return {"xml_tag": xml_tag, "node_type": "action", "ports": ports}

        if language == 'bash':
            ports = [{"direction": "input", "key": key.lower()} for key in self._BASH_INPUT_RE.findall(source)]
            ports += [{"direction": "output", "key": key.lower()} for key in self._BASH_OUTPUT_RE.findall(source)]
            return {"xml_tag": self._pascal_case(name), "node_type": "action", "ports": ports}

        return {"xml_tag": self._pascal_case(name), "node_type": "action", "ports": []}

    def get_local_file_state(self):
        """Bidirectional file sync (see /home/scientist/.claude/plans/
        breezy-splashing-koala.md, Part 3): the robot's half of
        LOCAL_FILE_STATE -- hash-only, not full content, matching this
        feature's own "don't over-send" ethos (_get_custom_node_files_for_sync
        only ever sends node-exposed files). Django compares these against
        its own current + last-known-synced hashes and only ever asks back
        for full content (REQUEST_LOCAL_FILE_CONTENT) for names that
        actually disagree.

        Supersedes this method's own predecessor here: get_local_files()
        used to end by private_send-ing its raw content wrapped as
        {"save_aiml": result} unconditionally, on every call -- an
        uncontrolled, unconditional push with no hash/conflict awareness
        at all, exactly the silent-overwrite risk this whole feature exists
        to close. That side effect is gone; get_local_files() is now a
        pure read, and this is the only thing that talks to Django, on
        purpose, with actual conflict detection behind it.
        """
        local = self.get_local_files()

        custom_aiml_dir = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_aiml')
        custom_aiml_state = self._bootstrap_and_get_sync_state(custom_aiml_dir, local["custom_aiml"])

        custom_maps_dir = os.path.join(self.files["xparo_custom_evns_folder_path"], 'custom_maps')
        custom_maps_state = self._bootstrap_and_get_sync_state(custom_maps_dir, local["custom_maps"])

        # custom_node_files' hashes already live inline on manifest.json
        # (bootstrapped by sync_custom_node_files itself, called here with
        # no payload -- the same safe "reload whatever's already synced"
        # startup case, harmless to re-run).
        self.sync_custom_node_files()
        node_files_dir = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_node_files')
        node_files_manifest_path = os.path.join(node_files_dir, 'manifest.json')
        manifest = {}
        if os.path.exists(node_files_manifest_path):
            with open(node_files_manifest_path, 'r') as file:
                try:
                    manifest = json.load(file)
                except json.JSONDecodeError:
                    manifest = {}
        node_files_state = {}
        for name, entry in manifest.items():
            if entry.get('content_hash'):
                node_files_state[name] = {"content_hash": entry['content_hash'], "language": entry.get('language', '')}

        examples_manifest_path = os.path.join(node_files_dir, 'examples_manifest.json')
        examples_manifest = {}
        if os.path.exists(examples_manifest_path):
            with open(examples_manifest_path, 'r') as file:
                try:
                    examples_manifest = json.load(file)
                except json.JSONDecodeError:
                    examples_manifest = {}
        known_names = set(manifest.keys())
        # Only names Django already manages (manifest.json) are excluded
        # here -- deliberately NOT the shipped examples too. A file
        # dropped directly into a language folder (the "user is not going
        # to edit the build packages, he will edit or add files in the
        # main package" case) AND the git-tracked example scripts
        # themselves are both real, main-file content the dashboard
        # should be able to show/drag/edit -- "zero Django connection
        # required" describes how the examples register at boot, not a
        # promise that they stay invisible to Django forever once one
        # actually exists. Discovered here so LOCAL_FILE_STATE reports
        # their hash the same as any other tracked file, and Django's own
        # reconciliation (baseline="" since it's never seen this name)
        # naturally treats it as disk-changed-only -- see
        # resolve_local_file_content's ingest path, which creates a new
        # CustomFile (+ a best-effort CustomNodeDefinition) instead of
        # silently dropping it. Once adopted, sync_custom_node_files'
        # own shadow-check (compares content hashes, not just names)
        # recognizes this as the same file, not a real collision, and
        # stays quiet.
        for name, entry in self._discover_new_node_files(known_names).items():
            node_files_state[name] = {
                "content_hash": sync_hash.content_hash(entry["source"], entry["header_source"]),
                "language": entry["language"],
            }

        return {
            "device_id": self.local_database.unique_id,
            "custom_node_files": node_files_state,
            "custom_aiml": {name: {"content_hash": s["hash"]} for name, s in custom_aiml_state.items()},
            "custom_maps": {name: {"content_hash": s["hash"]} for name, s in custom_maps_state.items()},
        }

    def _bootstrap_and_get_sync_state(self, directory, current_content_by_name):
        """Bootstrap-on-read (Part 5): anything present on disk right now
        with no recorded baseline gets one computed from its CURRENT
        content and written back, never flagged as a conflict just
        because tracking is new."""
        state = self._read_sync_state(directory)
        changed = False
        for name, content in current_content_by_name.items():
            if name in state:
                continue
            state[name] = {"hash": sync_hash.content_hash(content), "synced_at": datetime.utcnow().isoformat()}
            changed = True
        if changed:
            self._write_sync_state(directory, state)
        return state

    def get_local_file_content(self, requested):
        """Bidirectional file sync (see /home/scientist/.claude/plans/
        breezy-splashing-koala.md, Part 5): the actual current content for
        whatever names Django asked for in REQUEST_LOCAL_FILE_CONTENT
        (`{"custom_node_files": [...], "custom_aiml": [...], "custom_maps": [...]}`).
        A name that's disappeared from disk since LOCAL_FILE_STATE was
        reported (deleted mid-reconciliation) is simply omitted -- Django
        treats a missing entry as "nothing to compare", not an error.
        """
        response = {
            "device_id": self.local_database.unique_id,
            "custom_node_files": {}, "custom_aiml": {}, "custom_maps": {},
        }

        node_files_folder = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_node_files')
        manifest_path = os.path.join(node_files_folder, 'manifest.json')
        manifest = {}
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as file:
                try:
                    manifest = json.load(file)
                except json.JSONDecodeError:
                    manifest = {}
        for name in requested.get("custom_node_files", []):
            entry = manifest.get(name)
            if entry is not None:
                extension = self._CUSTOM_NODE_FILE_EXTENSIONS.get(entry.get('language'))
                if extension is None:
                    continue
                source_path = os.path.join(node_files_folder, entry['language'], name + extension)
                try:
                    with open(source_path, 'r') as file:
                        source = file.read()
                except OSError:
                    continue
                response["custom_node_files"][name] = {"source": source, "header_source": entry.get('header_source', '')}
                continue

            # Not in manifest.json at all -- a file dropped directly into
            # a language folder (see _discover_new_node_files). Django has
            # never heard of this name, so it needs enough to bootstrap a
            # brand-new CustomFile (+ a best-effort CustomNodeDefinition)
            # from scratch, not just source/header_source.
            discovered = self._discover_new_node_files(set()).get(name)
            if discovered is None:
                continue
            metadata = self._detect_node_metadata(name, discovered["language"], discovered["source"])
            response["custom_node_files"][name] = {
                "source": discovered["source"],
                "header_source": discovered["header_source"],
                "language": discovered["language"],
                "detected_xml_tag": metadata["xml_tag"],
                "detected_node_type": metadata["node_type"],
                "detected_ports": metadata["ports"],
            }

        custom_aiml_dir = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_aiml')
        for name in requested.get("custom_aiml", []):
            path = os.path.join(custom_aiml_dir, name + '.xml')
            try:
                with open(path, 'r') as file:
                    content = file.read()
            except OSError:
                continue
            start = content.find('<BehaviorTree ID="MainTree">')
            if start != -1:
                start += len('<BehaviorTree ID="MainTree">')
                end = content.find('</BehaviorTree>', start)
                content = content[start:end].strip() if end != -1 else content
            response["custom_aiml"][name] = content

        custom_maps_dir = os.path.join(self.files["xparo_custom_evns_folder_path"], 'custom_maps')
        for name in requested.get("custom_maps", []):
            path = os.path.join(custom_maps_dir, name + '.env')
            try:
                with open(path, 'r') as file:
                    response["custom_maps"][name] = file.read()
            except OSError:
                continue

        return response

    def send_initial_data(self):
        # self.private_send(json.dumps({"initisilaze_api":{}}))
        self.local_database.dashboard_receive({"needed_robot_data":{"sent":True}},self.private_send)
        # This is the transport's on_connected callback (wired at
        # construction, below) -- fires once a connection is confirmed
        # usable, on first connect AND every reconnect. Flushing here is
        # what actually delivers a TASK_RESULT/history record that
        # _send_important_dict had to queue because the connection was
        # down at the moment a task finished (see that method's own
        # docstring, finding F5).
        self._flush_pending_important_sends()
        self._health_full_report_due = True
        if self.ad_manager is not None:
            self.ad_manager.on_connected()

    def setup_ads(self, display="off", node=None):
        """Start showing this robot's Ads Center ads. display is the
        xparo_ads_display launch argument: "native" (xparo's own player),
        "xpshell" (XP-shell's player, over ROS 2 -- needs `node`) or "off".
        Call before connect() so the first connection asks for the schedule."""
        from .ads import AdManager
        from .ads.backends import make_backend
        folder = os.path.join(self.tmp_folder, "xparo", self.project_id, "ads")
        self.ad_manager = AdManager(folder, send=self._try_send_dict, backend=make_backend(display, node),
                                    base_url=lambda: getattr(self.transport, 'website_base_url', None))
        self.ad_manager.start()
        return self.ad_manager

    ##################################
    ###### function override #########
    ##################################

    def call_message(self,message,**kwargs):
        print(f"fun waiting to overrite :- {message}")


    #################################################################################


if __name__ == "__main__":
    ai_brain = Engine("secret_key","project_id")
