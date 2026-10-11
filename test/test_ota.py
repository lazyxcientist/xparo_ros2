"""ota.py -- updating xparo from its own git checkout. Real git repos in a
temp dir (an "origin" and the robot's checkout inside a colcon-shaped
workspace); only colcon build itself is faked."""
import json
import os
import subprocess
from unittest.mock import patch

import pytest

from xparo import ota

_real_run = ota._run
_real_missing = ota.missing_system_packages


def git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                         env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    return out.stdout.strip()


def commit(repo, path, text, message):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def robot(tmp_path):
    """origin (where updates come from) + a workspace whose src/xparo is a
    clone of it, plus the build/ and install/ dirs colcon would leave."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    v1 = commit(upstream, "package.xml", "<package><name>xparo</name></package>", "v1")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(upstream), str(origin)], check=True)

    ws = tmp_path / "ws"
    src = ws / "src" / "xparo"
    subprocess.run(["git", "clone", "-q", str(origin), str(src)], check=True)
    (ws / "build" / "xparo").mkdir(parents=True)
    (ws / "build" / "xparo" / "CMakeCache.txt").write_text(f"CMAKE_HOME_DIRECTORY:INTERNAL={src}\n")
    module_dir = ws / "install" / "xparo" / "lib" / "python3.12" / "site-packages" / "xparo"
    module_dir.mkdir(parents=True)

    def push_update(message="v2", path="xparo/engine.py", text="new"):
        git(upstream, "pull", "-q", str(origin), "main")
        sha = commit(upstream, path, text, message)
        git(upstream, "push", "-q", str(origin), "main")
        return sha

    loc = ota.locate(str(module_dir))
    with patch.object(ota, "locate", return_value=loc), \
         patch.object(ota, "missing_system_packages", return_value=([], None)), \
         patch.object(ota.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
        yield {"ws": ws, "src": src, "loc": loc, "v1": v1, "push_update": push_update, "upstream": upstream}


def test_locate_finds_the_checkout_through_the_workspace(robot):
    loc = robot["loc"]
    assert loc["workspace"] == str(robot["ws"])
    assert loc["repo"] == str(robot["src"])
    assert loc["symlink_install"] is False


def test_locate_outside_a_workspace_explains_why(tmp_path):
    assert "colcon workspace" in ota.locate(str(tmp_path / "somewhere" / "xparo"))["reason"]


def test_status_up_to_date(robot):
    st = ota.status(fetch=True)
    assert st["available"] and st["fetched"]
    assert st["behind"] == 0 and not st["update_available"] and not st["can_update"]
    assert st["current"]["subject"] == "v1"


def test_status_lists_new_commits_after_a_fetch(robot):
    robot["push_update"]("Fix the rosbag watchdog")
    robot["push_update"]("Add OTA", path="xparo/ota.py")
    assert ota.status(fetch=False)["behind"] == 0  # not fetched yet
    st = ota.status(fetch=True)
    assert st["behind"] == 2 and st["can_update"]
    assert [c["subject"] for c in st["commits"]] == ["Add OTA", "Fix the rosbag watchdog"]
    assert st["latest"]["subject"] == "Add OTA"


def test_local_edits_only_block_when_the_update_touches_the_same_files(robot):
    (robot["src"] / "package.xml").write_text("<package><name>xparo</name><!-- local --></package>")
    robot["push_update"]("engine change", path="xparo/engine.py")
    st = ota.status(fetch=True)
    assert st["local_changes"] == ["package.xml"] and st["can_update"]

    robot["push_update"]("package change", path="package.xml", text="<package><name>xparo</name>2</package>")
    st = ota.status(fetch=True)
    assert st["blocked_by"] == ["package.xml"] and not st["can_update"]
    assert "package.xml" in st["block_reason"]


def test_a_robot_with_its_own_commits_is_not_updated(robot):
    commit(str(robot["src"]), "local.txt", "x", "robot-only commit")
    robot["push_update"]()
    st = ota.status(fetch=True)
    assert st["ahead"] == 1 and st["behind"] == 1 and not st["can_update"]


def test_unreachable_remote_is_reported(robot):
    git(robot["src"], "remote", "set-url", "origin", str(robot["ws"] / "nope.git"))
    st = ota.status(fetch=True)
    assert st["fetch_error"] and not st["fetched"]


class Collector:
    def __init__(self):
        self.progress, self.results, self.restarts = [], [], 0

    def run(self, req=None, **kwargs):
        def restart():
            self.restarts += 1
        kwargs.setdefault("restart", restart)
        ota.handle_update(req or {"request_id": "u1"}, self.progress.append, self.results.append, **kwargs)
        return self.results[-1]["OTA_RESULT"] if self.results else None


def test_update_moves_builds_marks_and_restarts(robot, tmp_path):
    new = robot["push_update"]("v2")
    marker = str(tmp_path / "state" / "ota_pending.json")
    c = Collector()
    with patch.object(ota, "_build", return_value=(True, "ok")) as build:
        result = c.run(marker_path=marker)
    assert result is None  # the restarted process reports the result
    assert c.restarts == 1
    assert git(robot["src"], "rev-parse", "HEAD") == new
    build.assert_called_once()
    steps = [(p["OTA_PROGRESS"]["step"], p["OTA_PROGRESS"]["state"]) for p in c.progress]
    assert ("build", "done") in steps and steps[-1] == ("restart", "running")
    assert json.load(open(marker))["to"] == new
    assert not ota.update_in_progress()

    # ...and the new process confirms it on connect.
    sent = []
    ota.report_after_restart(marker, sent.append)
    assert sent[0]["OTA_RESULT"]["success"] is True and sent[0]["OTA_RESULT"]["request_id"] == "u1"
    assert not os.path.exists(marker)


def test_a_failed_build_goes_back_to_the_previous_version(robot):
    robot["push_update"]("broken")
    c = Collector()
    with patch.object(ota, "_build", side_effect=[(False, "CMake Error: boom"), (True, "")]):
        result = c.run()
    assert result["success"] is False and result["rolled_back"] is True
    assert "boom" in result["log"]
    assert git(robot["src"], "rev-parse", "HEAD") == robot["v1"]
    assert c.restarts == 0


def test_new_system_packages_ask_for_sudo_before_changing_anything(robot):
    robot["push_update"]("needs a new dependency")
    c = Collector()
    with patch.object(ota, "missing_system_packages", return_value=(["ros-jazzy-turtle-tf2-py"], None)), \
         patch.object(ota, "_run", side_effect=lambda argv, *a, **k: (1, "sudo: a password is required")
                      if argv[0] == "sudo" else _real_run(argv, *a, **k)), \
         patch.object(ota, "_build") as build:
        result = c.run()
    assert result["needs_sudo_password"] is True and result["packages"] == ["ros-jazzy-turtle-tf2-py"]
    assert git(robot["src"], "rev-parse", "HEAD") == robot["v1"]  # nothing changed
    build.assert_not_called()


def test_with_the_sudo_password_the_packages_are_installed_then_it_updates(robot):
    new = robot["push_update"]("needs a new dependency")
    c = Collector()
    with patch.object(ota, "missing_system_packages", return_value=(["ros-jazzy-turtle-tf2-py"], None)), \
         patch.object(ota, "run_privileged", return_value=(0, "installed", False)) as priv, \
         patch.object(ota, "_build", return_value=(True, "")):
        c.run({"request_id": "u2", "sudo_password": "robotpw"})
    argv, _timeout, password = priv.call_args.args
    assert argv[:3] == ["apt-get", "install", "-y"] and argv[-1] == "ros-jazzy-turtle-tf2-py"
    assert password == "robotpw"
    assert git(robot["src"], "rev-parse", "HEAD") == new


def test_running_tasks_block_the_update(robot):
    robot["push_update"]()
    c = Collector()
    with patch.object(ota, "_build") as build:
        result = c.run(running_tasks=lambda: [{"run_id": "r1"}])
    assert result["success"] is False and "task" in result["message"]
    assert git(robot["src"], "rev-parse", "HEAD") == robot["v1"]
    build.assert_not_called()


def test_already_up_to_date(robot):
    result = Collector().run()
    assert result["success"] is True and result["up_to_date"] is True


def test_one_update_at_a_time(robot):
    robot["push_update"]()
    assert ota._update_lock.acquire(blocking=False)
    try:
        result = Collector().run()
    finally:
        ota._update_lock.release()
    assert "already running" in result["message"]


def test_without_a_restart_hook_it_says_to_restart(robot):
    robot["push_update"]()
    c = Collector()
    with patch.object(ota, "_build", return_value=(True, "")):
        result = c.run(restart=None)
    assert result["success"] and result["restart_needed"]




def test_missing_system_packages_reads_rosdeps_dry_run(robot):
    """Dry-run output format checked against real rosdep 0.27 on Jazzy;
    the package.xml comes from the given git ref, nothing on disk."""
    sample = ("#[apt] Installation commands:\n  sudo -H apt-get install -y ros-jazzy-turtle-tf2-py\n"
              "  sudo -H apt-get install -y sl\n")
    seen = []

    def fake_run(argv, *args, **kwargs):
        if argv[0] == "rosdep":
            xml = open(os.path.join(argv[3], "xparo", "package.xml")).read()
            seen.append(xml)
            return 0, sample
        return _real_run(argv, *args, **kwargs)

    with patch.object(ota, "_run", side_effect=fake_run):
        packages, note = _real_missing(robot["loc"], "HEAD")
    assert packages == ["ros-jazzy-turtle-tf2-py", "sl"] and note is None
    assert "<name>xparo</name>" in seen[0]
