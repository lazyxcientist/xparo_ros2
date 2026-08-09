# Running these tests

```
pytest -c ros_packages/pytest.ini ros_packages/src/xparo/test/
```

(or `cd ros_packages/` first, so `pytest.ini` there is auto-discovered).

## Why not `colcon build` + `colcon test`?

This sandbox has never had `ros_packages` built (no `build/`/`install/`/`log/`
dirs), and the logic these tests cover -- `xparo.database.XP_Database`,
`xparo.engine.Engine`, `xparo.transports.django_ws.DjangoWsTransport`,
`xparo.blackbox_manager.BlackboxOrchestrator` -- is
almost entirely pure-Python message/state handling, not anything that
depends on message generation or an installed package layout. `ros_packages/pytest.ini`'s
`pythonpath` setting makes these importable directly from source
(`import xparo.database`, etc.) with no build step. `xparo_ros.py` (the
actual `rclpy.Node` subclass) is the one file in this package that *isn't*
covered this way -- it calls `get_package_share_directory('xparo')` at
**module import time**, which only resolves once the package is actually
registered with the ament index via a real `colcon build`. Testing that
file needs either a real build or mocking `ament_index_python` at the
call site; neither is done yet.

## Why `-c ros_packages/pytest.ini` and not the repo-root `pytest.ini`?

The root `pytest.ini` sets `DJANGO_SETTINGS_MODULE` and scopes
`testpaths` to `apps/` -- wrong context for ROS2 code. This directory has
its own config for that reason.

## The plugin-conflict fix

A bare `pytest` invocation from this venv crashes with
`PluginValidationError`/`INTERNALERROR` before collecting anything --
`include-system-site-packages = true` pulls in two ROS2 pytest
entry-point plugins (`launch_testing`, `launch_ros`) that are incompatible
with this pytest version. `ros_packages/pytest.ini`'s
`addopts = -p no:launch_testing -p no:launch_ros` disables exactly those
two by their registered entry-point names and nothing else, so
`pytest-django`/other plugins still autoload normally. (A blanket
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` env var also works around the crash,
but it disables *all* autoloaded plugins including ones you do want --
prefer the targeted `-p no:` flags above, which are already baked into
`ros_packages/pytest.ini` so nobody needs to remember either workaround.)
