"""Where a robot keeps what it writes at run time: ~/.xparo (XPARO_HOME).

It used to be inside the colcon install tree (install/xparo/...), so a clean
rebuild or `rm -rf install` deleted recordings not yet uploaded, the outbox
of unsent results, the play log of ads (proof of play) and the robot's
identity. Package files (config defaults, the example nodes in
custom_behaviors) stay where they are: they belong to the package, and
synced trees come back from the server.

adopt() moves an old location's contents over once, on the first start of
a version that uses the new place.
"""
import logging
import os
import shutil

log = logging.getLogger("xparo")


def root():
    return os.path.expanduser(os.environ.get("XPARO_HOME") or "~/.xparo")


def project_dir(project_id):
    return os.path.join(root(), "xparo", project_id or "no-project")


def adopt(old, new):
    """Move what's in `old` into `new` (entries already in `new` win).
    Returns how many entries moved; never raises."""
    try:
        if not old or not os.path.isdir(old) or os.path.islink(old):
            return 0
        if os.path.realpath(old) == os.path.realpath(new):
            return 0
        os.makedirs(new, exist_ok=True)
        moved = 0
        for name in os.listdir(old):
            target = os.path.join(new, name)
            if os.path.exists(target):
                continue
            shutil.move(os.path.join(old, name), target)
            moved += 1
        if moved:
            log.warning("moved %d item(s) from %s to %s (kept outside the install tree now)", moved, old, new)
        try:
            os.rmdir(old)
        except OSError:
            pass
        return moved
    except Exception as exc:
        log.warning("couldn't move %s to %s: %s", old, new, exc)
        return 0
