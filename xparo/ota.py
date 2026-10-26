"""Over-the-air updates of the xparo package itself (dashboard OTA popup).

A robot runs xparo from a colcon workspace built from a git checkout of
xparo_ros2 (docs/robot/install.rst). An update is:

  1. fetch from the checkout's upstream branch and show what's new
  2. if the new package.xml needs system packages that aren't installed
     (rosdep dry run), ask for the robot's sudo password BEFORE changing
     anything, then apt-get install them
  3. fast-forward merge (never a merge commit, never touches local edits
     unless the incoming commits change those same files -- then it stops)
  4. colcon build --packages-select xparo, the same way the workspace was
     built (--symlink-install or not). A failed build goes back to the
     previous commit and rebuilds it.
  5. restart: xparo_ros.py re-executes its own process (same PID, same
     command line), so it works under `ros2 launch`, systemd or a
     terminal alike. The new process confirms the result on connect.

A rebuild is always needed: even with --symlink-install, the node's own
executable (install/xparo/lib/xparo/xparo_ros) is a copy, not a symlink.

No ROS imports; every command is an argv list (no shell).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from .connectivity import run_privileged

GIT_TIMEOUT_SEC = 90.0
DEPS_TIMEOUT_SEC = 900.0
BUILD_TIMEOUT_SEC = 1800.0
MAX_COMMITS_LISTED = 30
LOG_TAIL_LINES = 40

_update_lock = threading.Lock()


def update_in_progress():
    return _update_lock.locked()


def _env():
    # C locale for parseable output; git must never wait for a password
    # prompt on a headless robot.
    return {**os.environ, "LC_ALL": "C", "LANG": "C", "GIT_TERMINAL_PROMPT": "0"}


def _run(argv, timeout, cwd=None):
    """(returncode, combined output); returncode None if it couldn't run."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                              env=_env(), stdin=subprocess.DEVNULL)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return None, f"{argv[0]} is not installed"
    except Exception as e:
        return None, str(e)


def _git(repo, *args, timeout=GIT_TIMEOUT_SEC):
    return _run(["git", "-C", repo, *args], timeout)


def _tail(output, lines=LOG_TAIL_LINES):
    return "\n".join((output or "").strip().splitlines()[-lines:])


# ----------------------------------------------------------------------
# Where is this xparo installed from?
# ----------------------------------------------------------------------
def locate(module_dir=None):
    """{"workspace", "source", "repo", "symlink_install"} -- or {"reason"}
    when this xparo can't be updated from here.

    module_dir is this package's directory as imported (not resolved), e.g.
    <ws>/install/xparo/lib/python3.X/site-packages/xparo."""
    module_dir = module_dir or os.path.dirname(os.path.abspath(__file__))
    workspace = None
    parts = module_dir.split(os.sep)
    if "install" in parts:
        idx = len(parts) - 1 - parts[::-1].index("install")
        workspace = os.sep.join(parts[:idx]) or os.sep
    if workspace is None or not os.path.isdir(os.path.join(workspace, "install")):
        return {"reason": "This xparo isn't running from a colcon workspace install, so it can't rebuild itself."}

    source = None
    cache = os.path.join(workspace, "build", "xparo", "CMakeCache.txt")
    try:
        with open(cache, "r") as f:
            for line in f:
                if line.startswith("CMAKE_HOME_DIRECTORY:"):
                    source = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    if not source:
        # --symlink-install: the Python files point back into the source.
        source = os.path.dirname(os.path.realpath(module_dir))
    rc, out = _git(source, "rev-parse", "--show-toplevel", timeout=10)
    if rc != 0:
        return {"reason": "This xparo's source isn't a git checkout, so there's nowhere to update from."}
    return {
        "workspace": workspace, "source": source, "repo": out.strip(),
        "symlink_install": os.path.islink(os.path.join(module_dir, "engine.py")),
    }


def _commit(repo, ref):
    rc, out = _git(repo, "log", "-1", "--format=%H%x1f%h%x1f%s%x1f%an%x1f%cI", ref, timeout=10)
    if rc != 0 or not out.strip():
        return None
    full, short, subject, author, date = (out.strip().split("\x1f") + [""] * 5)[:5]
    return {"commit": full, "short": short, "subject": subject, "author": author, "date": date}


def summary():
    """Cheap (no network) -- for robot info, so the dashboard can disable
    the OTA button on a robot that can't update itself."""
    try:
        loc = locate()
        if "reason" in loc:
            return {"available": False, "reason": loc["reason"]}
        head = _commit(loc["repo"], "HEAD")
        rc, branch = _git(loc["repo"], "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
        return {"available": True, "reason": "", "commit": head["short"] if head else None,
                "branch": branch.strip() if rc == 0 else None}
    except Exception as e:
        return {"available": False, "reason": f"Couldn't check: {e}"}


def status(fetch=False, during_update=False):
    """Everything the OTA popup shows. fetch=True asks the remote first
    (needs internet); otherwise compares against the last fetch.
    during_update: called by the update itself (which holds the lock)."""
    st = {"available": False, "reason": "", "checked_at": time.time(), "fetched": False, "fetch_error": None,
          "update_running": update_in_progress() and not during_update, "update_available": False, "can_update": False,
          "block_reason": "", "commits": [], "local_changes": [], "blocked_by": []}
    loc = locate()
    if "reason" in loc:
        st["reason"] = loc["reason"]
        return st
    repo = loc["repo"]
    st.update(available=True, workspace=loc["workspace"], source=loc["source"], repo=repo,
              symlink_install=loc["symlink_install"])

    rc, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    st["branch"] = branch.strip() if rc == 0 else None
    rc, upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=10)
    if rc != 0:
        st["current"] = _commit(repo, "HEAD")
        st["block_reason"] = (f"The checkout on branch {st['branch']!r} has no upstream branch to update from."
                              if st["branch"] != "HEAD" else "The checkout is on a detached commit, not a branch.")
        return st
    upstream = upstream.strip()
    st["upstream"] = upstream
    remote, _, remote_branch = upstream.partition("/")
    rc, url = _git(repo, "remote", "get-url", remote, timeout=10)
    st["remote_url"] = url.strip() if rc == 0 else None

    if fetch:
        rc, out = _git(repo, "fetch", "--quiet", remote, remote_branch)
        st["fetched"] = rc == 0
        if rc != 0:
            st["fetch_error"] = _tail(out, 3) or "fetch failed"

    st["current"] = _commit(repo, "HEAD")
    st["latest"] = _commit(repo, "@{u}")
    behind = _git(repo, "rev-list", "--count", "HEAD..@{u}", timeout=10)
    ahead = _git(repo, "rev-list", "--count", "@{u}..HEAD", timeout=10)
    st["behind"] = int(behind[1].strip()) if behind[0] == 0 else 0
    st["ahead"] = int(ahead[1].strip()) if ahead[0] == 0 else 0
    rc, out = _git(repo, "log", f"-{MAX_COMMITS_LISTED}", "--format=%h%x1f%s%x1f%an%x1f%cI", "HEAD..@{u}", timeout=10)
    if rc == 0:
        for line in out.strip().splitlines():
            short, subject, author, date = (line.split("\x1f") + [""] * 4)[:4]
            st["commits"].append({"short": short, "subject": subject, "author": author, "date": date})

    rc, out = _git(repo, "status", "--porcelain", "--untracked-files=no", timeout=20)
    if rc == 0:
        st["local_changes"] = [line[3:].split(" -> ")[-1] for line in out.splitlines() if len(line) > 3]
    rc, out = _git(repo, "diff", "--name-only", "HEAD...@{u}", timeout=20)
    incoming = set(out.split()) if rc == 0 else set()
    st["blocked_by"] = sorted(set(st["local_changes"]) & incoming)

    st["update_available"] = st["behind"] > 0
    if st["update_running"]:
        st["block_reason"] = "An update is already running on this robot."
    elif st["ahead"] and st["behind"]:
        st["block_reason"] = (f"The robot's checkout has {st['ahead']} commit(s) of its own that aren't upstream, "
                              f"so it can't simply move to the new version. Sort that out on the robot first.")
    elif st["blocked_by"]:
        st["block_reason"] = ("Files changed on the robot are also changed by the update: "
                              + ", ".join(st["blocked_by"]) + ". Commit or undo them on the robot first.")
    elif not shutil.which("colcon"):
        st["block_reason"] = "colcon isn't installed on the robot, so it can't rebuild xparo."
    st["can_update"] = st["update_available"] and not st["block_reason"]
    return st


def handle_get_status(fetch, send_response):
    send_response({"OTA_STATUS": status(fetch=fetch)})


# ----------------------------------------------------------------------
# System dependencies of the new version
# ----------------------------------------------------------------------
_APT_RE = re.compile(r"apt-get install -y (.+)$")


def missing_system_packages(loc, ref="@{u}"):
    """apt packages the package.xml at `ref` needs that aren't installed
    ([] when none, or when rosdep can't tell -- then the build will say).
    Read straight from git, so nothing on disk changes."""
    if not shutil.which("rosdep"):
        return [], "rosdep isn't installed, so new system dependencies can't be checked."
    rel = os.path.relpath(os.path.join(loc["source"], "package.xml"), loc["repo"])
    rc, xml = _git(loc["repo"], "show", f"{ref}:{rel}", timeout=10)
    if rc != 0:
        return [], None
    tmp = tempfile.mkdtemp(prefix="xparo_ota_deps_")
    try:
        os.makedirs(os.path.join(tmp, "xparo"))
        with open(os.path.join(tmp, "xparo", "package.xml"), "w") as f:
            f.write(xml)
        argv = ["rosdep", "install", "--from-paths", tmp, "--ignore-src", "-r", "-s", "-y"]
        if os.environ.get("ROS_DISTRO"):
            argv += ["--rosdistro", os.environ["ROS_DISTRO"]]
        rc, out = _run(argv, 120)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    packages = []
    for line in out.splitlines():
        match = _APT_RE.search(line.strip())
        if match:
            packages += match.group(1).split()
    note = None
    if "has not been initialized" in out or "rosdep update" in out:
        note = "rosdep isn't set up on the robot (run `sudo rosdep init && rosdep update`), so new dependencies weren't checked."
    return sorted(set(packages)), note


# ----------------------------------------------------------------------
# The update
# ----------------------------------------------------------------------
def _pending_marker(path, data=None):
    if data is None:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def report_after_restart(marker_path, send_result):
    """Called on (re)connect by the new process: confirms an update that
    restarted xparo, then forgets it."""
    marker = _pending_marker(marker_path)
    if not marker:
        return
    try:
        os.remove(marker_path)
    except OSError:
        pass
    loc = locate()
    head = _commit(loc["repo"], "HEAD") if "repo" in loc else None
    ok = bool(head) and head["commit"] == marker.get("to")
    send_result({"OTA_RESULT": {
        "request_id": marker.get("request_id"), "success": ok, "restarted": True,
        "from": marker.get("from_short"), "to": head["short"] if head else None,
        "message": (f"Updated to {head['short']} ({head['subject']}) and restarted." if ok else
                    "xparo restarted, but it isn't running the version the update installed."),
    }})


def handle_update(req, send_progress, send_result, restart=None, running_tasks=lambda: [],
                  marker_path=None):
    """req: {request_id, sudo_password?}. send_progress gets OTA_PROGRESS
    steps; send_result exactly one OTA_RESULT (unless xparo restarts --
    then the new process sends it, see report_after_restart)."""
    req = req or {}
    request_id = req.get("request_id")
    sudo_password = req.get("sudo_password") or None

    def progress(step, message, state="running", log=None):
        send_progress({"OTA_PROGRESS": {"request_id": request_id, "step": step, "state": state,
                                        "message": message, "log": log}})

    def result(success, message, **extra):
        send_result({"OTA_RESULT": {"request_id": request_id, "success": success, "message": message, **extra}})

    if not _update_lock.acquire(blocking=False):
        result(False, "An update is already running on this robot.")
        return
    restarting = False
    try:
        progress("check", "Checking for the new version...")
        st = status(fetch=True, during_update=True)
        if not st["available"]:
            return result(False, st["reason"])
        if st["fetch_error"]:
            return result(False, f"Couldn't reach {st.get('remote_url') or 'the update server'}: {st['fetch_error']}")
        if not st["update_available"]:
            return result(True, f"Already up to date ({st['current']['short']}).", up_to_date=True)
        if st["block_reason"]:
            return result(False, st["block_reason"])
        runs = running_tasks() or []
        if runs:
            return result(False, f"{len(runs)} task(s) are running on the robot. Wait for them to finish "
                                 f"(or cancel them) -- updating restarts xparo.")
        loc = {k: st[k] for k in ("workspace", "source", "repo", "symlink_install")}
        old, new = st["current"], st["latest"]
        progress("check", f"{old['short']} -> {new['short']} ({st['behind']} new commit(s))", state="done")

        packages, note = missing_system_packages(loc)
        if note:
            progress("dependencies", note, state="done")
        if packages:
            if not sudo_password:
                rc, _ = _run(["sudo", "-n", "true"], 10)
                if rc != 0:
                    return result(False, (f"The new version needs {len(packages)} system package(s): "
                                          f"{', '.join(packages)}. Enter the robot's sudo password to install them."),
                                  needs_sudo_password=True, packages=packages)
            progress("dependencies", f"Installing {', '.join(packages)}...")
            rc, out, needs_sudo = run_privileged(
                ["apt-get", "install", "-y", "-o", "Dpkg::Options::=--force-confold", *packages],
                DEPS_TIMEOUT_SEC, sudo_password)
            if rc != 0:
                if needs_sudo:
                    return result(False, "The robot rejected that sudo password." if sudo_password else
                                  "Installing the new dependencies needs the robot's sudo password.",
                                  needs_sudo_password=True, packages=packages)
                return result(False, "Installing the new dependencies failed. Nothing else was changed.",
                              log=_tail(out))
            progress("dependencies", f"Installed {', '.join(packages)}.", state="done")

        progress("download", f"Moving to {new['short']}...")
        # Written before anything changes and removed once the new version
        # is built: if the power goes in between, the next `ros2 launch`
        # finds it and puts the previous version back (launch/xparo_launch.py).
        write_journal({"repo": loc["repo"], "workspace": loc["workspace"], "from": old["commit"],
                       "to": new["commit"], "symlink_install": bool(loc.get("symlink_install")), "at": time.time()})
        rc, out = _git(loc["repo"], "merge", "--ff-only", "@{u}")
        if rc != 0:
            clear_journal()
            return result(False, "Couldn't move the checkout to the new version. Nothing was changed.",
                          log=_tail(out))
        progress("download", f"Now at {new['short']}: {new['subject']}", state="done")

        progress("build", "Building xparo with colcon (this can take a few minutes)...")
        ok, out = _build(loc)
        if not ok:
            progress("build", "Build failed -- going back to the previous version.", state="failed", log=_tail(out))
            _git(loc["repo"], "reset", "--keep", old["commit"])
            rebuilt, _ = _build(loc)
            if rebuilt:
                clear_journal()
            return result(False, (f"The new version didn't build, so the robot went back to {old['short']}"
                                  + ("" if rebuilt else " (rebuilding that also failed -- check the robot)") + "."),
                          log=_tail(out), rolled_back=True)
        progress("build", "Built.", state="done")
        clear_journal()

        if restart is None:
            return result(True, f"Updated to {new['short']}. Restart xparo on the robot to start using it.",
                          restart_needed=True)
        if marker_path:
            _pending_marker(marker_path, {"request_id": request_id, "from": old["commit"],
                                          "from_short": old["short"], "to": new["commit"], "at": time.time()})
        progress("restart", "Restarting xparo with the new version -- it will be back online in a moment.")
        restarting = True
    finally:
        _update_lock.release()
    if restarting:
        restart()


def journal_path():
    """Outside the install tree on purpose (a broken build can't touch it).
    XPARO_OTA_JOURNAL overrides it (tests)."""
    return os.environ.get("XPARO_OTA_JOURNAL") or os.path.join(os.path.expanduser("~"), ".xparo", "ota_journal.json")


def write_journal(entry, path=None):
    path = path or journal_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entry, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def clear_journal(path=None):
    try:
        os.remove(path or journal_path())
    except FileNotFoundError:
        pass


def _build(loc):
    argv = ["colcon", "build", "--packages-select", "xparo"]
    if loc.get("symlink_install"):
        argv.append("--symlink-install")
    # The robot keeps running (navigation, sensors) while it builds: at
    # normal priority the build could starve those control loops.
    if shutil.which("nice"):
        argv = ["nice", "-n", "15"] + argv
    rc, out = _run(argv, BUILD_TIMEOUT_SEC, cwd=loc["workspace"])
    return rc == 0, out
