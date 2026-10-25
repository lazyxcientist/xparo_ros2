"""Synced project files belong to one project.

Trees, maps, inline nodes, tasks and plugin paths synced from the dashboard
live in shared folders (custom_behaviors/, custom_envs/), not per project.
After switching a robot to another project, the old project's files were
reported to the new one as "edited on the robot", copied into it, and its
tasks stayed runnable from /xparo/run_task. Now the folders remember which
project they belong to; on a switch the old project's files are moved
aside (never deleted) into custom_behaviors/.previous_projects/<old id>/.
"""
import os
import shutil
import time

MARKER = ".xparo_project_id"
PROJECT_ITEMS = [  # (folder key, name inside it)
    ("behaviors", "custom_aiml"),
    ("behaviors", "inline_nodes"),
    ("behaviors", "tasks.json"),
    ("behaviors", "plugin_paths.json"),
    ("behaviors", os.path.join("custom_node_files", "manifest.json")),
    ("behaviors", os.path.join("custom_node_files", "sync_state.json")),
    ("envs", "custom_maps"),
]


def retire_other_project_files(behaviors_dir, envs_dir, project_id):
    """Returns the folder the previous project's files were moved to, or
    None when there was nothing to do."""
    if not project_id:
        return None
    marker = os.path.join(behaviors_dir, MARKER)
    try:
        with open(marker) as f:
            previous = f.read().strip()
    except OSError:
        previous = None
    moved_to = None
    if previous and previous != project_id:
        moved_to = os.path.join(behaviors_dir, ".previous_projects", f"{previous}-{int(time.time())}")
        for key, name in PROJECT_ITEMS:
            base = behaviors_dir if key == "behaviors" else envs_dir
            source = os.path.join(base, name)
            if os.path.exists(source):
                target = os.path.join(moved_to, key, name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(source, target)
    os.makedirs(behaviors_dir, exist_ok=True)
    with open(marker, "w") as f:
        f.write(project_id)
    return moved_to
