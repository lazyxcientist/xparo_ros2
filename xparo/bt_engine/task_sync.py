"""Local task execution: Django syncs every task in a project down to its
connected robots (apps/analytics/data_analyis.py's _get_custom_tasks ->
engine.py's "custom_tasks" sync branch -> sync_custom_tasks, persisted
here the same way rosbag_config.json already is) so a task can be
triggered directly on the robot -- publishing its task_id on
/xparo/run_task (xparo_ros.py) -- without any further Django contact at
trigger time. This module resolves a locally-cached task_id into the
exact {task_id, tree_xml, blackboard, save_task_history} shape
run_task.handle_run_task expects, the same shape Django's own RUN_TASK
dispatch sends over the websocket -- handle_run_task itself is unchanged
and doesn't know or care which path produced it.

tree_xml resolution mirrors apps/analytics/data_analyis.py's
_resolve_tree_xml exactly ('' -> the project's single default tree, else
a key into custom_aiml), just reading this robot's own already-synced
local files (config/default.xml, custom_behaviors/<name>.xml) instead of
the project's DB rows -- those files exist specifically because engine.py
already persists aiml/custom_aiml sync to disk, wrapped in a full
<root><BehaviorTree ID="MainTree">...</BehaviorTree></root> document (see
engine.py's own "aiml"/"custom_aiml" on_ws_message branches); this module
un-wraps that back into the bare fragment tree_builder.build_tree expects
(the same extraction engine.py's own get_local_files already does when
sending local files back up to Django).

Blackboard resolution mirrors apps/analytics/data_analyis.py's
resolve_blackboard_mapping exactly (same five mapping types, same
semantics), resolved from this robot's own already-synced local files
(custom_envs/<file>.env for the 'env' type) instead of the project's DB --
this repo and the Django one are separate git repos, so, like
xml_parser.py's own wrap/strip convention, the logic is independently
duplicated here rather than shared.
"""
import json
import os
import random
import secrets

TASKS_FILENAME = "tasks.json"

_BT_START_MARKER = '<BehaviorTree ID="MainTree">'
_BT_END_MARKER = "</BehaviorTree>"


def load_custom_tasks(custom_behaviors_folder_path):
    """Never raises -- {} if nothing has been synced yet (a robot that
    hasn't connected to Django since this feature shipped, or one running
    fully offline)."""
    pth = os.path.join(custom_behaviors_folder_path, TASKS_FILENAME)
    try:
        with open(pth, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _extract_behavior_tree_fragment(full_xml):
    start = full_xml.find(_BT_START_MARKER)
    if start == -1:
        return full_xml
    start += len(_BT_START_MARKER)
    end = full_xml.find(_BT_END_MARKER, start)
    if end == -1:
        return full_xml
    return full_xml[start:end].strip()


def resolve_tree_xml(behaviour_tree_name, files):
    """`files` is Engine.files (the same dict every other sync branch in
    engine.py already reads/writes through)."""
    if behaviour_tree_name:
        path = os.path.join(files["xparo_custom_behaviors_folder_path"], f"{behaviour_tree_name}.xml")
    else:
        path = files["behavior"]
    if not os.path.exists(path):
        return ""
    with open(path, "r") as file:
        return _extract_behavior_tree_fragment(file.read())


def _parse_env_value(content, key):
    for line in (content or "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def resolve_blackboard(blackboard_mapping, params, override_params, files):
    override_params = override_params or {}
    params_by_id = {p.get("id"): p for p in (params or [])}
    resolved = {}
    for var_name, entry in (blackboard_mapping or {}).items():
        mapping_type = entry.get("mapping_type")
        if mapping_type == "default":
            resolved[var_name] = entry.get("value", "")
        elif mapping_type == "env":
            env_path = os.path.join(
                files["xparo_custom_evns_folder_path"], f"{entry.get('env_file', '')}.env"
            )
            content = ""
            if os.path.exists(env_path):
                with open(env_path, "r") as file:
                    content = file.read()
            value = _parse_env_value(content, entry.get("env_var", ""))
            if value is not None:
                resolved[var_name] = value
        elif mapping_type == "task_param":
            param = params_by_id.get(entry.get("param_id"))
            if param is not None:
                resolved[var_name] = override_params.get(param.get("name"), param.get("default_value", ""))
        elif mapping_type == "random_number":
            try:
                lo, hi = int(entry.get("min", 0)), int(entry.get("max", 0))
                resolved[var_name] = random.randint(lo, hi) if hi >= lo else lo
            except (TypeError, ValueError):
                pass
        elif mapping_type == "random_string":
            try:
                length = max(1, int(entry.get("length", 8)))
            except (TypeError, ValueError):
                length = 8
            resolved[var_name] = secrets.token_urlsafe(length)[:length]
    return resolved


def build_run_task_val(task_id, override_params, custom_tasks, files):
    """Looks task_id up in the already-synced local cache and returns the
    val shape run_task.handle_run_task expects, or None if task_id isn't
    in the local cache (stale client state, or this robot hasn't synced
    since the task was created -- logged by the caller, not an exception,
    matching this repo's "safe no-op over missing live state" posture
    elsewhere, e.g. Engine.bt_executor being None)."""
    task = custom_tasks.get(task_id)
    if task is None:
        return None
    tree_xml = resolve_tree_xml(task.get("behaviour_tree_name", ""), files)
    blackboard = resolve_blackboard(
        task.get("blackboard_mapping"), task.get("params"), override_params, files,
    )
    return {
        "task_id": task_id,
        "tree_xml": tree_xml,
        "blackboard": blackboard,
        "save_task_history": task.get("save_task_history", True),
        # TaskStageChoices value synced down with everything else above --
        # see run_task.py's ALLOWED_TASK_STAGES check, which applies here
        # exactly like it does to a Django-dispatched RUN_TASK. Defaults
        # to "development" (a stale locally-cached task from before this
        # feature has no "stage" key at all) -- the task's own stage, not
        # a robot's, so an unlabeled task should only be trusted by the
        # most permissive robots, not treated as production-ready. See
        # run_task.py's handle_run_task for the same reasoning in detail.
        "stage": task.get("stage") or "development",
    }
