"""Rosbag recording state machine -- ported from tethered_module's
rosbag_node.py (a standalone BagControlNode(Node) there) into a composable
class the Xparo rclpy node owns alongside everything else it does, per the
XPARO fleet-unification plan's decision to merge tethered_module's genuinely
more robust recording strategy into xparo rather than keeping two separate
implementations.

Why this one and not blackbox_manager.py's old subprocess.Popen("ros2 bag
record", ...): that approach has no way to know if recording is *actually*
happening (a died/hung subprocess looks identical to a healthy one from
Python's side), no pause/resume, and no retry logic. This drives the native
rosbag2_recorder node through its service interface instead (Record / Stop /
Resume / IsPaused / IsDiscoveryRunning / SplitBagfile) with verify-after-
action and a watchdog, so start_recording()/stop_recording() actually know
whether they worked.

Bug history preserved from the original (still applies here):
  v1 -> v2: Record() opens paused, not writing (fixed: record->resume->verify).
            Record.srv's return_code wasn't checked (fixed: checked explicitly).
  v2 -> v3: is_paused alone cannot distinguish CLOSED from PAUSED. Stop()
            internally sets the paused flag true as part of shutdown and
            never resets it, so after a real Stop, is_paused keeps
            returning paused=True forever -- indistinguishable from a
            session that's merely paused-but-still-open. Confirmed from
            recorder logs: Stop() always logs "Topics discovery stopped."
            right before "Pausing recording." / "Recording stopped", while
            a fresh Record() always logs "Topics discovery started."
            immediately. So CLOSED-vs-OPEN is now determined by
            is_discovery_running, and is_paused is only consulted once we
            already know a session is open.

Single-mcap-file mode: the launch side (xparo_launch.py) starts the
rosbag2_recorder process with --start-paused and no --max-bag-duration /
--max-bag-size, so nothing splits a session into multiple files by time or
size -- Resume/Stop below are what actually start/end a single continuous
recording. save/split is therefore disabled in control_cb (split_bagfile
always creates a second file, incompatible with that). handle_split() is
kept but unreachable in case this requirement changes later.

Confirmed interfaces (from the original's bench testing):
  Record.srv             : string uri  ---  int32 return_code, string error_string
  Stop.srv                : (empty)     ---  (empty)
  SplitBagfile             : (empty)     ---  (empty)
  IsPaused.srv             : (empty)     ---  bool paused
  Resume.srv               : (empty)     ---  (empty)
  IsDiscoveryRunning.srv   : (empty)     ---  bool running

State determination (two-signal check):
  discovery.running == False                        -> CLOSED
  discovery.running == True, is_paused.paused == True  -> PAUSED
  discovery.running == True, is_paused.paused == False -> WRITING

KNOWN LIMITATION: this assumes discovery is only ever toggled by this
class's own record()/stop() calls (true as long as the recorder is launched
without --no-discovery). If discovery is ever started/stopped independently,
or the recorder is launched with discovery disabled, this heuristic breaks
and needs a different signal.
"""

import datetime
import json
import os

from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from std_msgs.msg import String, Bool
from rosbag2_interfaces.srv import (
    Record, Stop, SplitBagfile, IsPaused, Resume, IsDiscoveryRunning,
)

# Recording config synced from Project_Dashboard (apps/analytics/
# data_analyis.py's GET_rosbag_config/EDIT_rosbag_config), persisted here
# under the same custom_behaviors/ folder every other synced config
# (plugin_paths.json, inline_nodes/) already lives in. Two independent
# readers: xparo_ros.py reads start_mode/start_delay_seconds *before*
# constructing RosbagControl (its __init__ kicks off the boot sequence
# immediately, so this can't wait for Engine's own post-construction sync
# the way BT plugin config does); xparo_launch.py reads record_all/
# ignore_topics/include_topics to build `ros2 bag record`'s command line
# for the *next* launch -- topic selection is a subprocess launch
# argument, not something re-appliable to an already-running recorder.
ROSBAG_CONFIG_FILENAME = 'rosbag_config.json'
DEFAULT_ROSBAG_CONFIG = {
    'record_all': True,
    'ignore_topics': [],
    'include_topics': [],
    'start_mode': 'auto',
    'start_delay_seconds': 0,
}


def load_rosbag_config(custom_behaviors_folder_path):
    """Never raises -- a missing/corrupt config file falls back to
    DEFAULT_ROSBAG_CONFIG (record everything, auto-start immediately),
    same posture as every other synced-config loader in this package."""
    path = os.path.join(custom_behaviors_folder_path, ROSBAG_CONFIG_FILENAME)
    if not os.path.exists(path):
        return dict(DEFAULT_ROSBAG_CONFIG)
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return {**DEFAULT_ROSBAG_CONFIG, **data}
    except Exception:
        return dict(DEFAULT_ROSBAG_CONFIG)


RELIABLE_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
)

SERVICE_WAIT_SEC = 2.0
MAX_RETRIES = 3
WATCHDOG_PERIOD_SEC = 5.0
STATUS_PERIOD_SEC = 1.0
POST_ACTION_VERIFY_DELAY_SEC = 0.3   # let the recorder settle before re-checking

UNKNOWN = 'unknown'   # haven't confirmed real state yet -- refuse to guess
CLOSED = 'closed'     # no open session
PAUSED = 'paused'     # session open, not writing
WRITING = 'writing'   # session open, actively writing


class RosbagControl:
    """`node` is the host rclpy Node (Xparo) -- every ROS primitive below is
    created on it, not on this class, so it shares the node's executor/
    context instead of needing (and conflicting with) one of its own.
    `bag_dir` replaces the original's hardcoded BAG_ROOT (a path specific
    to the machine it was developed on) -- pass the same BAG_DIR Engine
    already resolves (ROS param, per-project default) so recordings land
    next to everything else this robot writes.
    """

    def __init__(self, node, bag_dir, start_mode='auto', start_delay_seconds=0):
        self.node = node
        self.bag_dir = bag_dir
        # 'auto' -- auto-start recording at boot (after the delay below,
        # 0 meaning immediately). 'task' -- stay closed at boot and wait
        # for something external (the /ros2_bag_control topic today; a
        # future BT node later) to call handle_start() explicitly.
        self.start_mode = start_mode
        self.start_delay_seconds = max(0, start_delay_seconds or 0)
        self._start_delay_timer = None

        self.state = UNKNOWN
        self.recorder_alive = False

        self.record_client = node.create_client(Record, '/rosbag2_recorder/record')
        self.stop_client = node.create_client(Stop, '/rosbag2_recorder/stop')
        self.resume_client = node.create_client(Resume, '/rosbag2_recorder/resume')
        self.split_client = node.create_client(SplitBagfile, '/rosbag2_recorder/split_bagfile')
        self.is_paused_client = node.create_client(IsPaused, '/rosbag2_recorder/is_paused')
        self.is_discovery_client = node.create_client(
            IsDiscoveryRunning, '/rosbag2_recorder/is_discovery_running')

        node.create_subscription(String, '/ros2_bag_control', self.control_cb, RELIABLE_QOS)
        self.status_pub = node.create_publisher(String, '/ros2_bag_control/recording_status', RELIABLE_QOS)
        self.alive_pub = node.create_publisher(Bool, '/ros2_bag_control/recorder_alive', RELIABLE_QOS)

        node.create_timer(WATCHDOG_PERIOD_SEC, self.watchdog_cb)
        node.create_timer(STATUS_PERIOD_SEC, self.publish_status)

        node.get_logger().info('Bag control up. Verifying recorder state...')
        # Confirmed empirically (ros2 bag record, Jazzy): the recorder
        # process ALWAYS auto-opens a session at its own launch time (the
        # -o path baked into xparo_launch.py's ExecuteProcess), landing in
        # PAUSED (not CLOSED) even with --start-paused -- it does not wait
        # idle for a first Record call. Record.srv also rejects outright
        # ("already recording") if a session is already open, regardless
        # of paused/writing. So this boot-time session must be explicitly
        # closed before handle_start() can ever open a real, correctly-
        # timestamped one -- otherwise the first "recording" a user starts
        # would silently just resume writing into that throwaway launch-
        # time path. handle_stop() is idempotent (no-ops if already
        # CLOSED), so this is safe to always run.
        #
        # _boot_sequence (not a bare self.handle_stop) is the fix for a
        # real race: this used to hand handle_stop straight to resync's
        # on_done, while a completely separate, uncoordinated caller
        # (BlackboxOrchestrator.start_recording(), from XP_Database
        # .__init__, whenever record_bags=True) called handle_start() of
        # its own accord moments later. Both chains are async -- nothing
        # blocks waiting for rclpy to start spinning yet at this point in
        # boot -- so whichever service call happened to land last on the
        # recorder silently won, with no error anywhere. Confirmed live:
        # a real boot opened a session, resumed it, then had it
        # immediately closed and compressed again by this constructor's
        # own cleanup landing after the resume. Auto-starting is now
        # entirely this class's own decision (see _boot_sequence/
        # _maybe_auto_start below), chained strictly *after* the cleanup
        # confirms CLOSED, never racing it -- XP_Database no longer calls
        # start_recording() from its own __init__ at all.
        self.resync(on_done=self._boot_sequence)

    def _boot_sequence(self):
        self.handle_stop(on_done=self._maybe_auto_start)

    def _maybe_auto_start(self):
        if self.start_mode != 'auto':
            self.node.get_logger().info(
                'Recording set to "wait for task" -- staying closed until explicitly started.')
            return
        if self.start_delay_seconds > 0:
            self.node.get_logger().info(f'Auto-starting recording in {self.start_delay_seconds}s...')
            self._start_delay_timer = self.node.create_timer(self.start_delay_seconds, self._fire_delayed_start)
            return
        self.handle_start()

    def _fire_delayed_start(self):
        # One-shot: cancel first, same pattern _verify_after already uses
        # for its own delayed re-check, since create_timer is periodic by
        # default and there's no separate one-shot primitive here.
        if self._start_delay_timer is not None:
            self._start_delay_timer.cancel()
            self._start_delay_timer = None
        self.handle_start()

    # ---------- the single source of truth (two-signal check) ----------

    def resync(self, on_done=None):
        """Determine real state via is_discovery_running, then is_paused. Never guess."""
        if not self.is_discovery_client.wait_for_service(timeout_sec=SERVICE_WAIT_SEC):
            self.recorder_alive = False
            self.state = UNKNOWN
            self.node.get_logger().error('is_discovery_running service unreachable -- state UNKNOWN.')
            if on_done:
                on_done()
            return

        future = self.is_discovery_client.call_async(IsDiscoveryRunning.Request())

        def _discovery_done(f):
            try:
                discovery_res = f.result()
                self.recorder_alive = True
            except Exception as e:
                self.recorder_alive = False
                self.state = UNKNOWN
                self.node.get_logger().error(f'is_discovery_running call failed ({e}) -- state UNKNOWN.')
                if on_done:
                    on_done()
                return

            if not discovery_res.running:
                # No open session.
                self._set_state(CLOSED, on_done)
                return

            # Session is open -- now check whether it's actively writing.
            if not self.is_paused_client.wait_for_service(timeout_sec=SERVICE_WAIT_SEC):
                self.node.get_logger().error('is_paused unreachable while discovery is running -- state UNKNOWN.')
                self._set_state(UNKNOWN, on_done)
                return

            paused_future = self.is_paused_client.call_async(IsPaused.Request())

            def _paused_done(pf):
                try:
                    paused_res = pf.result()
                    new_state = PAUSED if paused_res.paused else WRITING
                    self._set_state(new_state, on_done)
                except Exception as e:
                    self.node.get_logger().error(f'is_paused call failed ({e}) -- state UNKNOWN.')
                    self._set_state(UNKNOWN, on_done)

            paused_future.add_done_callback(_paused_done)

        future.add_done_callback(_discovery_done)

    def _set_state(self, new_state, on_done):
        if self.state != UNKNOWN and new_state != self.state:
            self.node.get_logger().warn(f'State mismatch: node had {self.state}, recorder reports {new_state}. Correcting.')
        self.state = new_state
        if on_done:
            on_done()

    def _verify_after(self, expected_state, ok_msg, on_done=None):
        """One-shot delayed re-check, so we confirm reality instead of trusting a response.
        on_done (optional) fires after the check, regardless of whether it
        matched -- used to chain a further action (e.g. _boot_sequence
        starting recording only once the boot-session close is verified
        CLOSED, not merely requested).
        """
        def _check():
            timer.cancel()
            self.resync(on_done=lambda: self._log_verify_result(expected_state, ok_msg, on_done))
        timer = self.node.create_timer(POST_ACTION_VERIFY_DELAY_SEC, _check)

    def _log_verify_result(self, expected_state, ok_msg, on_done=None):
        if self.state == expected_state:
            self.node.get_logger().info(ok_msg)
        else:
            self.node.get_logger().error(
                f'VERIFICATION MISMATCH: expected {expected_state} after action, '
                f'recorder actually reports {self.state}. Investigate immediately.'
            )
        if on_done:
            on_done()

    # ---------- watchdog / status ----------

    def watchdog_cb(self):
        was_alive = self.recorder_alive
        self.resync()
        if was_alive and not self.recorder_alive:
            self.node.get_logger().error(
                'RECORDER UNREACHABLE. Expect auto-respawn per launch file into a new '
                'segment. Previously closed segments remain valid.'
            )
        elif not was_alive and self.recorder_alive:
            self.node.get_logger().info('Recorder reachable again.')

    def publish_status(self):
        s = String()
        s.data = self.state
        self.status_pub.publish(s)
        a = Bool()
        a.data = self.recorder_alive
        self.alive_pub.publish(a)

    # ---------- commands ----------

    def control_cb(self, msg: String):
        cmd = msg.data.strip().lower()
        if cmd == 'start':
            self.handle_start()
        elif cmd == 'stop':
            self.handle_stop()
        elif cmd in ('save', 'split'):
            self.node.get_logger().warn(
                'save/split is DISABLED: single-mcap-file mode is required, and '
                'split_bagfile always creates a second file by definition. '
                'No action taken.'
            )
        elif cmd == 'status':
            self.node.get_logger().info(f'state={self.state}, recorder_alive={self.recorder_alive}')
        else:
            self.node.get_logger().warn(f'Unknown command "{cmd}" -- ignored, no state change.')

    def handle_start(self):
        if self.state == UNKNOWN:
            # Retries itself once resync() resolves a real state, rather
            # than just bailing -- matters because start_recording() can
            # legitimately be called (e.g. from XP_Database.__init__, via
            # BlackboxOrchestrator) before the host node's executor has
            # started spinning, i.e. before resync()'s own async service
            # calls have had any chance to resolve. Bounded by real
            # service-call/timeout delays each cycle (SERVICE_WAIT_SEC),
            # not a tight loop.
            self.node.get_logger().error('State unknown -- re-syncing before acting.')
            self.resync(on_done=self.handle_start)
            return
        if self.state == WRITING:
            self.node.get_logger().info('Already recording -- skipping start.')
            return
        if self.state == PAUSED:
            self.node.get_logger().info('Session open but paused -- resuming.')
            self._call(self.resume_client, Resume.Request(), 'resume',
                       on_ok=lambda: self._verify_after(WRITING, 'Recording resumed.'))
            return

        # CLOSED -> open a brand new session. Always an absolute path (under
        # self.bag_dir) so every session lands in the same known place
        # regardless of the recorder process's launch cwd.
        req = Record.Request()
        req.uri = os.path.join(
            self.bag_dir, 'bag_' + datetime.datetime.now().strftime('%Y_%m_%d-%H_%M_%S')
        )

        def on_record_response(res):
            if res.return_code != 0:
                self.node.get_logger().error(f'Record REJECTED: {res.error_string}. State left as-is.')
                self.resync()
                return
            self.node.get_logger().info('Session opened, resuming to start writing...')
            self._call(self.resume_client, Resume.Request(), 'resume',
                       on_ok=lambda: self._verify_after(WRITING, 'Recording started.'))

        self._call_checked(self.record_client, req, 'record', on_response=on_record_response)

    def handle_stop(self, on_done=None):
        if self.state == CLOSED:
            self.node.get_logger().info('Not recording -- skipping stop.')
            if on_done:
                on_done()
            return
        if self.state == UNKNOWN:
            self.node.get_logger().warn('State unknown -- attempting stop anyway to avoid a dangling session.')
        self._call(self.stop_client, Stop.Request(), 'stop',
                   on_ok=lambda: self._verify_after(CLOSED, 'Stop issued, bag should be finalized.', on_done=on_done))

    def handle_split(self):
        """DISABLED -- unreachable from control_cb. Kept in case single-file
        mode requirement changes later. split_bagfile always creates a
        second file, incompatible with the current one-file requirement."""
        if self.state != WRITING:
            self.node.get_logger().warn(f'Not actively writing (state={self.state}) -- nothing meaningful to save.')
            return
        self._call(self.split_client, SplitBagfile.Request(), 'split_bagfile',
                   on_ok=lambda: self.node.get_logger().info('Checkpoint saved (bag split).'))

    # ---------- generic call helpers ----------

    def _call(self, client, request, name, on_ok, retries_left=MAX_RETRIES):
        if not client.wait_for_service(timeout_sec=SERVICE_WAIT_SEC):
            if retries_left > 0:
                self.node.get_logger().warn(f'{name} service not ready, retrying ({retries_left} left)...')
                self._call(client, request, name, on_ok, retries_left - 1)
            else:
                self.node.get_logger().error(f'{name} FAILED: service unreachable after retries.')
                self.resync()
            return

        future = client.call_async(request)

        def _done(f):
            try:
                f.result()
                on_ok()
            except Exception as e:
                if retries_left > 0:
                    self.node.get_logger().warn(f'{name} call failed ({e}), retrying ({retries_left} left)...')
                    self._call(client, request, name, on_ok, retries_left - 1)
                else:
                    self.node.get_logger().error(f'{name} FAILED after retries: {e}')
                    self.resync()

        future.add_done_callback(_done)

    def _call_checked(self, client, request, name, on_response, retries_left=MAX_RETRIES):
        """Like _call, but hands the actual response (with return_code) to the caller."""
        if not client.wait_for_service(timeout_sec=SERVICE_WAIT_SEC):
            if retries_left > 0:
                self.node.get_logger().warn(f'{name} service not ready, retrying ({retries_left} left)...')
                self._call_checked(client, request, name, on_response, retries_left - 1)
            else:
                self.node.get_logger().error(f'{name} FAILED: service unreachable after retries.')
                self.resync()
            return

        future = client.call_async(request)

        def _done(f):
            try:
                res = f.result()
                on_response(res)
            except Exception as e:
                if retries_left > 0:
                    self.node.get_logger().warn(f'{name} call failed ({e}), retrying ({retries_left} left)...')
                    self._call_checked(client, request, name, on_response, retries_left - 1)
                else:
                    self.node.get_logger().error(f'{name} FAILED after retries: {e}')
                    self.resync()

        future.add_done_callback(_done)
