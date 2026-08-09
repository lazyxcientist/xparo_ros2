# XPARO
this version is currently tested and suported with ros2 humble 

you can modify https://github.com/lazyxcientist/xparo_ros/xparo_ros/xpar.py to make supported with other ros distro easily

```
    ██╗░░██╗██████╗░░█████╗░██████╗░░█████╗░
    ╚██╗██╔╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗
    ░╚███╔╝░██████╔╝███████║██████╔╝██║░░██║
    ░██╔██╗░██╔═══╝░██╔══██║██╔══██╗██║░░██║
    ██╔╝╚██╗██║░░░░░██║░░██║██║░░██║╚█████╔╝
    ╚═╝░░╚═╝╚═╝░░░░░╚═╝░░╚═╝╚═╝░░╚═╝░╚════╝░
        Live   your   DREAMS   parrallely
```



website: https://xparo-website.me/

github: https://github.com/lazyxcientist/xparo_ros

email:   xpassistantpersonal@gmail.com

Looking to exercise everything this package's API can do (remote
exec, teleop, file transfer, rosbag, credentials, ...) against a real
running robot? See [TESTING.md](TESTING.md). For the automated `pytest`
suite, see [test/README.md](test/README.md).



<br>

-------------
## getting started with X.P.A.R.O
-------------

step 1 : go to https://xparo-website.me/dashboard_app and create an new project by clicking on "add new" button.

step 2 : now go to your project 

step 3 : copy the project_id (or secret_key if any) of your project. 

step 4 : copy the code given below and paste your keys there.





-------------
## how to use xparo with ros2
-------------

#### baisc setup
```bash
cd your_workspace/src  # move to src , where all packages are placed
git clone https://github.com/lazyxcientist/xparo_ros.git
```



#### build package
```bash
cd your_workspace  # move to workspace
rosdep install --from-paths src --ignore-src -r -y
colcon build    # build the workspace
source install/setup.bash
```

#### run the ros2_node
```bash
ros2 run xparo xparo
```


install requiremnts = [requirements.txt](./requirements.txt)

topic list  

- /xparo/ask
- /xparo/dashboard/receive
- /xparo/dashboard/send
- /xparo/for_custom_llm
- /xparo/response


send command 

    ros2 topic pub /xparo/ask std_msgs/msg/String "data: 'your question here'" 



check the output

    ros2 topic echo /xparo/response 

------------------
## parameters

`xparo_secret_key = your key here`

`xparo_project_id = your project key here`

`xparo_connection_type = hybrid`

`xparo_custom_aiml_path = /src/core/aiml`

`xparo_custom_sets_path = /src/core/aiml_sets`

`xparo_custom_maps_path = /src/core/aiml_maps`



