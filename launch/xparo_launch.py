import os
from ament_index_python.packages import get_package_share_directory

from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    # config = os.path.join(
    #     get_package_share_directory('xparo'),
    #     'config',
    #     'xparo_params.yaml',
    # )

    return LaunchDescription([
        # Node(
        #     package='xparo',
        #     executable='xparo_c',
        #     name='xparo_c',
        #     output='screen',
        #     parameters=[config],
        # ),
        Node(
            package='xparo',
            executable='xparo_ros',
            name='xparo',
            output='screen',
            # parameters=[config],
        ),
    ])
