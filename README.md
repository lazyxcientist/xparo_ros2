# XPARO

```
    ██╗░░██╗██████╗░░█████╗░██████╗░░█████╗░
    ╚██╗██╔╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗
    ░╚███╔╝░██████╔╝███████║██████╔╝██║░░██║
    ░██╔██╗░██╔═══╝░██╔══██║██╔══██╗██║░░██║
    ██╔╝╚██╗██║░░░░░██║░░██║██║░░██║╚█████╔╝
    ╚═╝░░╚═╝╚═╝░░░░░╚═╝░░╚═╝╚═╝░░╚═╝░╚════╝░
        Live   your   DREAMS   parrallely
```

The ROS 2 package that connects a robot to [XPARO](https://xparo.in): it
keeps the robot online on your project's dashboard, runs the behaviour trees
and tasks you build there, and gives the dashboard remote tools (terminal,
teleop, files, Wi-Fi, ROS bag recording, health).

**Full documentation: <https://xparo.in/docs/>**

Tested on **Ubuntu 24.04 with ROS 2 Jazzy**.

## Quick start

1. Sign in at <https://xparo.in/dashboard> and press **New project**.
2. Open the project. In the **Secret Key Manager** on its **Dashboard** page,
   copy the **Project Key** (your project ID), then **Generate** a secret key
   and copy it (it is shown only once).
3. On the robot:

```bash
sudo apt install -y git python3-websocket python3-psutil python3-yaml python3-requests ros-jazzy-py-trees

mkdir -p ~/xparo_ws/src && cd ~/xparo_ws/src
git clone https://github.com/lazyxcientist/xparo_ros2.git
cd ~/xparo_ws
source /opt/ros/jazzy/setup.bash
colcon build --packages-select xparo

source install/setup.bash
ros2 launch xparo xparo_launch.py \
  xparo_project_id:=<your-project-id> \
  xparo_secret_key:=<your-secret-key> \
  xparo_stage:=development
```

The robot appears on the dashboard's **Robots Fleet** page as online. On its
first connection it receives its own credential and saves it, so later starts
don't depend on the secret key.

`xparo_stage:=development` lets the robot run tasks still in development
(every new task starts there). Leave it out on robots that should only run
production tasks.

## Learn more

| | |
|---|---|
| [Install and run](https://xparo.in/docs/robot/install.html) | Step by step, with the messages you should see |
| [Configuration](https://xparo.in/docs/robot/configuration.html) | Every launch parameter, with a command builder |
| [ROS 2 topics](https://xparo.in/docs/robot/topics.html) | `/xparo/ask`, `/xparo/run_task`, `/joy`, ... |
| [Behaviour trees](https://xparo.in/docs/robot/behaviour-trees.html) | Nodes, expressions and run results |
| [Custom nodes](https://xparo.in/docs/robot/custom-nodes.html) | Python, C++, JavaScript and Bash nodes |
| [Troubleshooting](https://xparo.in/docs/robot/troubleshooting.html) | When the robot doesn't show up |

For developers of this package: [TESTING.md](TESTING.md) exercises the remote
features against a running robot, and [test/README.md](test/README.md)
describes the `pytest` suite.

Contact: xpassistantpersonal@gmail.com
