import os
import re
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.conditions import IfCondition
from launch_ros.actions import Node
from launch.substitutions import LaunchConfiguration

from xparo.rosbag_control import load_rosbag_config


def _rosbag_record_topic_args(custom_behaviors_folder_path):
    """Builds the topic-selection portion of `ros2 bag record`'s command
    line from whatever config was last synced from the dashboard (Sensors
    Database page -> apps/analytics/data_analyis.py's EDIT_rosbag_config
    -> engine.py's sync_rosbag_config -> persisted here). This can only
    ever reflect this robot's *next* launch, never an already-running
    recorder -- `ros2 bag record`'s topic list is a process launch
    argument, not something re-appliable at runtime the way
    RosbagControl's own Record/Stop/Resume service calls are.

    Returns a single string to splice into the same bash -c command this
    file already builds (matching --start-paused/--storage/etc.'s own
    plain-string-concatenation style just below).
    """
    config = load_rosbag_config(custom_behaviors_folder_path)
    if config['record_all']:
        ignore = [t for t in (config['ignore_topics'] or []) if t]
        if not ignore:
            return '-a'
        # `ros2 bag record -a --exclude '<regex>'` -- exact-topic-name
        # alternation, anchored so e.g. "/tf" doesn't also swallow
        # "/tf_static".
        pattern = '|'.join(f'^{re.escape(t)}$' for t in ignore)
        return f"-a --exclude '{pattern}'"
    include = [t for t in (config['include_topics'] or []) if t]
    if not include:
        # Nothing explicitly selected -- fall back to recording
        # everything rather than silently writing an empty bag forever,
        # which would look identical to "recording is broken" from the
        # Sensors Database page.
        return '-a'
    return ' '.join(include)


def generate_launch_description():
    # "xparo_ros" is the node's executable name (see the Node(executable=...)
    # below), not the package name -- get_package_share_directory needs the
    # actual ROS2 package name from package.xml/CMakeLists.txt's project(),
    # which is "xparo". Was wrong here since before this session; only
    # surfaced now because nothing had actually run `ros2 launch` against a
    # real colcon build before.
    current_dir = get_package_share_directory("xparo")


    xparo_project_id =                  LaunchConfiguration('xparo_project_id',                     default="")
    xparo_secret_key =                  LaunchConfiguration('xparo_secret_key',                     default="")
    xparo_connection_type =             LaunchConfiguration('xparo_connection_type',                default="websocket")
    # "production" (xparo.in) or "local" (127.0.0.1:8000, for developing
    # against a local Django server) -- see transports/django_ws.py's
    # DEFAULT_ENVIRONMENT.
    xparo_environment =                 LaunchConfiguration('xparo_environment',                    default="production")
    xparo_behavior_path =               LaunchConfiguration('xparo_behavior_path',                  default=os.path.join(current_dir,'config','default.xml'))
    xparo_file_path =                   LaunchConfiguration('xparo_file_path',                      default=os.path.join(current_dir,'config','default.yaml'))
    xparo_env_path =                    LaunchConfiguration('xparo_env_path',                       default=os.path.join(current_dir,'config','default.env'))
    xparo_local_env_path =              LaunchConfiguration('xparo_local_env_path',                 default=os.path.join(current_dir, 'config', 'default.env'))
    xparo_properties_path =             LaunchConfiguration('xparo_properties_path',                default=os.path.join(current_dir,'properties','properties.txt'))
    xparo_custom_behaviors_folder_path= LaunchConfiguration('xparo_custom_behaviors_folder_path',   default=os.path.join(current_dir,'custom_behaviors'))
    xparo_custom_files_folder_path =    LaunchConfiguration('xparo_custom_files_folder_path',       default=os.path.join(current_dir,'custom_files'))
    xparo_custom_evns_folder_path =     LaunchConfiguration('xparo_custom_evns_folder_path',        default=os.path.join(current_dir,'custom_envs'))
    record_bags =                       LaunchConfiguration('record_bags',                          default=False)
    # Must be shared between the xparo_ros node and the rosbag2 recorder
    # process below -- RosbagControl (rosbag_control.py) opens sessions
    # under this exact path via the Record service, so both sides need to
    # agree on it. Declared as its own launch arg (rather than left to
    # xparo_ros.py's internal per-project default) specifically so that
    # agreement is guaranteed rather than coincidental.
    bag_dir =                           LaunchConfiguration('BAG_DIR',                               default=os.path.join(current_dir,'ros_bags'))
    # "django_ws" (networked robots) or "tethered_tcp" (a physically-
    # tethered ROV with no path to Django) -- see
    # transports/tethered_tcp.py's module docstring.
    xparo_transport =                   LaunchConfiguration('xparo_transport',                       default="django_ws")
    # Only read when xparo_transport=="tethered_tcp" -- path to a copy of
    # config/tethered_channels.yaml with this deployment's actual
    # host/port pairs. Empty default falls back to loopback test values.
    tethered_channels_config_path =     LaunchConfiguration('tethered_channels_config_path',         default="")


    ####################################



    return LaunchDescription([
            ######################################
            #### parameters
            DeclareLaunchArgument('xparo_project_id',                   default_value=xparo_project_id,                 description='your project key'),
            DeclareLaunchArgument('xparo_secret_key',                   default_value=xparo_secret_key,                 description='your genrated secret key'),
            DeclareLaunchArgument('xparo_connection_type',              default_value=xparo_connection_type,            description='websocket or restframework connection'),
            DeclareLaunchArgument('xparo_environment',                  default_value=xparo_environment,                description='"production" (xparo.in) or "local" (127.0.0.1:8000)'),
            DeclareLaunchArgument('xparo_behavior_path',                default_value=xparo_behavior_path,              description='path for AUTO-genrated behaviour tree.'),
            DeclareLaunchArgument('xparo_file_path',                    default_value=xparo_file_path,                  description='path for AUTO-genrated file'),
            DeclareLaunchArgument('xparo_env_path',                     default_value=xparo_env_path,                   description='path for AUTO-genrated env/blackbord'),
            DeclareLaunchArgument('xparo_local_env_path',               default_value=xparo_local_env_path,             description='path for your local .env file, which you can change from server.'),
            DeclareLaunchArgument('xparo_properties_path',              default_value=xparo_properties_path,            description='path for AUTO-genrated properties'),
            DeclareLaunchArgument('xparo_custom_behaviors_folder_path', default_value=xparo_custom_behaviors_folder_path,description='path where your all custom behaviour trees are saved'),
            DeclareLaunchArgument('xparo_custom_files_folder_path',     default_value=xparo_custom_files_folder_path,   description='path where your all custom files are saved'),
            DeclareLaunchArgument('xparo_custom_evns_folder_path',      default_value=xparo_custom_evns_folder_path,    description='path where your all custom evns are saved'),
            DeclareLaunchArgument('record_bags',                        default_value=record_bags,                      description='record ros bag or not.'),
            DeclareLaunchArgument('BAG_DIR',                            default_value=bag_dir,                          description='where rosbag sessions are written; shared with the rosbag2 recorder process.'),
            DeclareLaunchArgument('xparo_transport',                    default_value=xparo_transport,                  description='"django_ws" or "tethered_tcp"'),
            DeclareLaunchArgument('tethered_channels_config_path',      default_value=tethered_channels_config_path,    description='path to a tethered_channels.yaml, only used when xparo_transport=="tethered_tcp"'),



            ###############################################
            Node(
                package='xparo',
                executable='xparo_ros',
                name='xparo_ros',
                parameters=[{
                            'xparo_project_id':                     xparo_project_id,
                            'xparo_secret_key':                     xparo_secret_key,
                            'xparo_connection_type':                xparo_connection_type,
                            'xparo_environment':                    xparo_environment,
                            'xparo_behavior_path':                  xparo_behavior_path,
                            'xparo_file_path':                      xparo_file_path,
                            'xparo_env_path':                       xparo_env_path,
                            'xparo_local_env_path':                 xparo_local_env_path,
                            'xparo_properties_path':                xparo_properties_path,
                            'xparo_custom_behaviors_folder_path':   xparo_custom_behaviors_folder_path,
                            'xparo_custom_files_folder_path':       xparo_custom_files_folder_path,
                            'xparo_custom_evns_folder_path':        xparo_custom_evns_folder_path,
                            'record_bags':                          record_bags,
                            'BAG_DIR':                               bag_dir,
                            'xparo_transport':                      xparo_transport,
                            'tethered_channels_config_path':        tethered_channels_config_path,

                            }],
                output='screen'),
            ###############################################

            # rosbag2's own recorder process -- RosbagControl
            # (rosbag_control.py, held by the xparo_ros node above) drives
            # it entirely through its service interface (Record/Stop/
            # Resume/IsPaused/IsDiscoveryRunning), the same interface any
            # `ros2 bag record` process exposes regardless of how it's
            # invoked. Only started when record_bags:=true.
            #
            # --start-paused: confirmed empirically that the recorder
            # always auto-opens a session at its own launch (there's no
            # way to have it start fully idle) -- RosbagControl closes this
            # one out as its first action (see rosbag_control.py's
            # __init__) before ever opening a real, timestamped session via
            # the Record service, so --start-paused just avoids wasting a
            # few seconds of real topic data into a throwaway boot file in
            # the meantime.
            #
            # No --max-bag-size / --max-bag-duration: single-mcap-file-per-
            # session mode (matches tethered_module's rosbag_node.py, which
            # this control logic was ported from) -- see rosbag_control.py's
            # module docstring for the durability tradeoff this implies and
            # how --compression-mode file/--compression-format zstd
            # (kept from xparo's own prior defaults) partially offsets it.
            #
            # -o's boot_session_$(date ...) suffix must be a literal shell
            # command substitution (run via `bash -c`, not a launch-time
            # Substitution) so it re-expands to a fresh, unique path on
            # every respawn -- otherwise a respawn would always try to
            # reuse the exact same boot-session directory as the previous
            # run and `ros2 bag record` refuses to write into one that
            # already exists, permanently breaking respawn recovery after
            # the very first crash. bag_dir itself IS a launch-time
            # Substitution (a LaunchConfiguration, possibly CLI-overridden)
            # so it's resolved once, up front, and concatenated in as a
            # plain string alongside the still-literal $(date ...) part.
            ExecuteProcess(
                condition=IfCondition(record_bags),
                cmd=['bash', '-c', [
                    # Reads whatever topic-selection config was last
                    # synced from the dashboard, from the *default*
                    # custom_behaviors_folder_path -- like current_dir
                    # above, this doesn't account for a CLI override of
                    # xparo_custom_behaviors_folder_path itself (a
                    # LaunchConfiguration substitution, not a concrete
                    # value yet at this point in generate_launch_description,
                    # and not worth the extra complexity to resolve early
                    # for what would be a rare override of an already-rare
                    # override).
                    f'ros2 bag record {_rosbag_record_topic_args(os.path.join(current_dir, "custom_behaviors"))} -o ', bag_dir,
                    '/boot_session_$(date +%Y%m%d_%H%M%S)'
                    ' --start-paused --storage mcap'
                    ' --compression-mode file --compression-format zstd',
                ]],
                output='screen',
                respawn=True,
                respawn_delay=2.0,
                name='rosbag2_recorder_process',
            ),
            ###############################################


    ])
