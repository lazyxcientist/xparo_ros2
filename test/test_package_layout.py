"""2026-10-31 field test: a fresh `git clone` of this package failed the very
first install step (`colcon build --packages-select xparo`, as
docs/robot/install.rst says) with

    CMake Error at cmake_install.cmake: file INSTALL cannot find ".../custom_envs"

CMakeLists.txt installs custom_envs/ and custom_files/, but neither had a
tracked file -- they only existed on the author's disk. Every directory
CMakeLists.txt installs must therefore exist in git.
"""
import os
import re
import subprocess
from unittest.mock import patch

import pytest

PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _installed_directories():
    cmake = open(os.path.join(PACKAGE, "CMakeLists.txt")).read()
    cmake = re.sub(r"#[^\n]*", "", cmake)
    dirs = []
    for block in re.findall(r"install\s*\(\s*DIRECTORY\s+(.*?)\)", cmake, flags=re.S):
        for token in block.split():
            if token in ("DESTINATION", "FILES_MATCHING", "PATTERN", "USE_SOURCE_PERMISSIONS"):
                break
            dirs.append(token.rstrip("/"))
    return dirs


def _tracked_files(directory):
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", directory], cwd=PACKAGE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_cmake_installs_some_directories():
    assert {"config", "custom_behaviors", "custom_envs", "custom_files"} <= set(_installed_directories())


@pytest.mark.parametrize("directory", _installed_directories())
def test_every_installed_directory_is_in_git(directory):
    assert _tracked_files(directory), (
        f"CMakeLists.txt installs {directory}/ but git would include no file from it: "
        f"a fresh clone fails colcon build (add a .gitkeep)")


def test_first_start_without_local_env_is_quiet(tmp_path, capsys):
    """A fresh install has no config/local.env yet; that is normal and must not
    print a raw "[Errno 2] No such file or directory" or send anything."""
    from xparo.engine import Engine
    engine = Engine("secret", "proj-layout-test", connection_type="offline")
    engine.files["local_env"] = str(tmp_path / "config" / "local.env")
    with patch.object(engine, "private_send") as send:
        assert engine.get_initial_local_env_data() == ""
    send.assert_not_called()
    assert "No such file" not in capsys.readouterr().out


def test_existing_local_env_is_sent(tmp_path):
    from xparo.engine import Engine
    engine = Engine("secret", "proj-layout-test", connection_type="offline")
    env = tmp_path / "local.env"
    env.write_text("SPEED=0.4\n")
    engine.files["local_env"] = str(env)
    with patch.object(engine, "private_send") as send:
        engine.get_initial_local_env_data()
    payload = send.call_args[0][0]
    assert "SPEED=0.4" in payload and "ADD_robots_maps" in payload
