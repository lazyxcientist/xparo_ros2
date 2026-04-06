import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch_ros.actions import Node
from launch.substitutions import LaunchConfiguration



def generate_launch_description():
    current_dir = get_package_share_directory("xparo_ros")


    xparo_project_id =                  LaunchConfiguration('xparo_project_id',                     default="")
    xparo_secret_key =                  LaunchConfiguration('xparo_secret_key',                     default="")
    xparo_connection_type =             LaunchConfiguration('xparo_connection_type',                default="websocket")
    xparo_behavior_path =               LaunchConfiguration('xparo_behavior_path',                  default=os.path.join(current_dir,'config','default.xml'))
    xparo_file_path =                   LaunchConfiguration('xparo_file_path',                      default=os.path.join(current_dir,'config','default.yaml'))
    xparo_env_path =                    LaunchConfiguration('xparo_env_path',                       default=os.path.join(current_dir,'config','default.env'))
    xparo_local_env_path =              LaunchConfiguration('xparo_local_env_path',                 default=os.path.join(current_dir, 'config', 'default.env'))
    xparo_properties_path =             LaunchConfiguration('xparo_properties_path',                default=os.path.join(current_dir,'properties','properties.txt'))
    xparo_custom_behaviors_folder_path= LaunchConfiguration('xparo_custom_behaviors_folder_path',   default=os.path.join(current_dir,'custom_behaviors'))
    xparo_custom_files_folder_path =    LaunchConfiguration('xparo_custom_files_folder_path',       default=os.path.join(current_dir,'custom_files'))
    xparo_custom_evns_folder_path =     LaunchConfiguration('xparo_custom_evns_folder_path',        default=os.path.join(current_dir,'custom_envs'))
    record_bags =                       LaunchConfiguration('record_bags',                          default=False)


    ####################################



    return LaunchDescription([
            ######################################
            #### parameters
            DeclareLaunchArgument('xparo_project_id',                   default_value=xparo_project_id,                 description='your project key'),
            DeclareLaunchArgument('xparo_secret_key',                   default_value=xparo_secret_key,                 description='your genrated secret key'),
            DeclareLaunchArgument('xparo_connection_type',              default_value=xparo_connection_type,            description='websocket or restframework connection'),
            DeclareLaunchArgument('xparo_behavior_path',                default_value=xparo_behavior_path,              description='path for AUTO-genrated behaviour tree.'),
            DeclareLaunchArgument('xparo_file_path',                    default_value=xparo_file_path,                  description='path for AUTO-genrated file'),
            DeclareLaunchArgument('xparo_env_path',                     default_value=xparo_env_path,                   description='path for AUTO-genrated env/blackbord'),
            DeclareLaunchArgument('xparo_local_env_path',               default_value=xparo_local_env_path,             description='path for your local .env file, which you can change from server.'),
            DeclareLaunchArgument('xparo_properties_path',              default_value=xparo_properties_path,            description='path for AUTO-genrated properties'),
            DeclareLaunchArgument('xparo_custom_behaviors_folder_path', default_value=xparo_custom_behaviors_folder_path,description='path where your all custom behaviour trees are saved'),
            DeclareLaunchArgument('xparo_custom_files_folder_path',     default_value=xparo_custom_files_folder_path,   description='path where your all custom files are saved'),
            DeclareLaunchArgument('xparo_custom_evns_folder_path',      default_value=xparo_custom_evns_folder_path,    description='path where your all custom evns are saved'),
            DeclareLaunchArgument('record_bags',                        default_value=record_bags,                      description='record ros bag or not.'),



            ###############################################
            Node(
                package='xparo',
                executable='xparo_ros',
                name='xparo_ros',
                parameters=[{
                            'xparo_project_id':                     xparo_project_id,
                            'xparo_secret_key':                     xparo_secret_key,
                            'xparo_connection_type':                xparo_connection_type,
                            'xparo_behavior_path':                  xparo_behavior_path,
                            'xparo_file_path':                      xparo_file_path,
                            'xparo_env_path':                       xparo_env_path,
                            'xparo_local_env_path':                 xparo_local_env_path,
                            'xparo_properties_path':                xparo_properties_path,
                            'xparo_custom_behaviors_folder_path':   xparo_custom_behaviors_folder_path,
                            'xparo_custom_files_folder_path':       xparo_custom_files_folder_path,
                            'xparo_custom_evns_folder_path':        xparo_custom_evns_folder_path,
                            'record_bags':                          record_bags,
                            
                            }],
                output='screen'),
            ###############################################
        

    ])

