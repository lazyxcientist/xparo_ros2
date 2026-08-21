import json
import threading
import time
import os
from datetime import datetime
from .database import XP_Database
from .transports.django_ws import DjangoWsTransport
from . import remote_ops
from .bt_engine import run_task
from .bt_engine import plugin_loader
from .bt_engine import runners

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
    def __init__(self,secret_key,project_id,connection_type = "websocket",record_bags=record_bags,BAG_DIR=None,environment=None,rosbag_control=None,joy_publish=None,xparo_transport="django_ws",tethered_channels_config=None,xparo_stage="production"):
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
        # bt_engine.executor.BehaviorTreeExecutor -- set post-construction
        # by xparo_ros.py, same reasoning as call_message/files just below
        # in Xparo.__init__: it needs this already-built Engine instance to
        # call add_live_update/add_task_history on, so it can't be built
        # (or passed in) before Engine exists. None here means RUN_TASK is
        # a safe no-op (matches TELEOP's own "no live node" default) rather
        # than an AttributeError, for Engine's standalone-outside-ROS2 use
        # (see this file's own __main__ block) and every test in this repo.
        self.bt_executor = None
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
        with open(self.xparo_credential_path, 'w') as file:
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
        """
        from .bt_engine.node_registry import NODE_REGISTRY

        folder = os.path.join(self.files["xparo_custom_behaviors_folder_path"], 'custom_node_files')
        manifest_path = os.path.join(folder, 'manifest.json')

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
                source_path = os.path.join(folder, name + extension)
                with open(source_path, 'w') as file:
                    file.write(entry.get('source', ''))
                if language == 'bash':
                    os.chmod(source_path, 0o755)
                manifest[name] = {
                    'language': language,
                    'xml_tag': entry.get('xml_tag', ''),
                    'node_type': entry.get('node_type', 'action'),
                    'ports': entry.get('ports', []),
                    'header_source': entry.get('header_source', ''),
                }

            for stale_name, stale_entry in previous_manifest.items():
                if stale_name in manifest:
                    continue
                stale_extension = self._CUSTOM_NODE_FILE_EXTENSIONS.get(stale_entry.get('language'), '')
                for suffix in (stale_extension, '.hpp'):
                    if not suffix:
                        continue
                    try:
                        os.remove(os.path.join(folder, stale_name + suffix))
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

        plugin_loader.unregister_tags(self._custom_node_file_tags)
        registered_tags = set()

        python_paths = [
            os.path.join(folder, name + '.py')
            for name, entry in manifest.items() if entry.get('language') == 'python'
        ]
        registered_tags |= set(plugin_loader.register_plugins(python_paths).keys())

        js_entries = [(name, entry) for name, entry in manifest.items() if entry.get('language') == 'javascript']
        if js_entries:
            js_runtime_dir = os.path.join(folder, 'js_runtime')
            runners.ensure_js_runtime(js_runtime_dir)
            for name, entry in js_entries:
                xml_tag = entry.get('xml_tag')
                if not xml_tag:
                    continue
                output_keys = [p['key'] for p in entry.get('ports', []) if p.get('direction') == 'output']
                NODE_REGISTRY[xml_tag] = runners.make_javascript_node_factory(
                    os.path.join(folder, name + '.js'), js_runtime_dir, output_keys,
                )
                registered_tags.add(xml_tag)

        for name, entry in manifest.items():
            if entry.get('language') != 'bash':
                continue
            xml_tag = entry.get('xml_tag')
            if not xml_tag:
                continue
            script_path = os.path.join(folder, name + '.sh')

            def _bash_builder(nm, attrs, blackboard, children, ros_node, script_path=script_path):
                return runners.BashProcessNode(nm, attrs, blackboard, script_path=script_path, ros_node=ros_node)

            NODE_REGISTRY[xml_tag] = _bash_builder
            registered_tags.add(xml_tag)

        cpp_entries = [(name, entry) for name, entry in manifest.items() if entry.get('language') == 'cpp']
        if cpp_entries:
            cpp_build_dir = os.path.join(folder, 'cpp_build')
            for name, entry in cpp_entries:
                xml_tag = entry.get('xml_tag')
                cpp_path = os.path.join(folder, name + '.cpp')
                if not xml_tag or not os.path.exists(cpp_path):
                    continue
                with open(cpp_path, 'r') as file:
                    source = file.read()
                output_keys = [p['key'] for p in entry.get('ports', []) if p.get('direction') == 'output']
                executable_path = runners.compile_cpp_node(source, entry.get('header_source', ''), cpp_build_dir, name)
                if executable_path is None:
                    continue
                NODE_REGISTRY[xml_tag] = runners.make_cpp_node_factory(executable_path, output_keys)
                registered_tags.add(xml_tag)

        self._custom_node_file_tags = registered_tags
        print(f"[bt_engine] loaded custom node file tags: {sorted(registered_tags)}")

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
            return
        custom_tasks = task_sync.load_custom_tasks(self.files["xparo_custom_behaviors_folder_path"])
        val = task_sync.build_run_task_val(task_id, override_params, custom_tasks, self.files)
        if val is None:
            print(f"[run_task_from_topic] task_id {task_id!r} not in the local sync cache -- "
                  f"either it doesn't exist, or this robot hasn't synced since it was created")
            return
        threading.Thread(
            target=run_task.handle_run_task,
            args=(self.bt_executor, val, self._send_dict),
            kwargs={"add_task_history": self.add_task_history, "xparo_stage": self.xparo_stage},
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
                self.sync_custom_node_files(val)
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
            elif k=="RUN_TASK":
                # bt_executor is None when this Engine isn't owned by a
                # live Xparo node (standalone use, or every test in this
                # repo) -- same "safe no-op, no crash" posture TELEOP's
                # default joy_publish already has for the same reason.
                if self.bt_executor is not None:
                    threading.Thread(
                        target=run_task.handle_run_task,
                        args=(self.bt_executor, val, self._send_dict),
                        kwargs={"add_task_history": self.add_task_history, "xparo_stage": self.xparo_stage},
                        daemon=True,
                    ).start()
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
