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
import re
import secrets
import time

from .. import sync_hash
from .xml_parser import TreeParseError, is_tree_document, parse_fragment, tree_file_content, tree_from_file_content

TASKS_FILENAME = "tasks.json"


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
    # Only XPARO's own one-tree wrapper is un-wrapped; a whole Groot2
    # document (MainTree + subtrees) is built whole, not cut to MainTree.
    return tree_from_file_content(full_xml)


def resolve_tree_xml(behaviour_tree_name, files):
    """`files` is Engine.files (the same dict every other sync branch in
    engine.py already reads/writes through).

    A named tree lives in custom_behaviors/custom_aiml/<name>.xml, not
    flat under custom_behaviors/ -- engine.py's own on_ws_message
    "custom_aiml" branch and get_local_file_content both write/read it
    from that same custom_aiml subfolder (see this feature's own
    directory-restructure/collision fix); resolving the old flat path
    here would find nothing for any task whose tree was ever actually
    synced through that mechanism, and silently return "" -- which
    tree_builder.build_tree then rejects as an "empty behaviour tree
    fragment", not a clear "couldn't find this tree" error.
    """
    if behaviour_tree_name:
        path = os.path.join(files["xparo_custom_behaviors_folder_path"], 'custom_aiml', f"{behaviour_tree_name}.xml")
    else:
        path = files["behavior"]
    if not os.path.exists(path):
        return ""
    with open(path, "r") as file:
        return _extract_behavior_tree_fragment(file.read())


def resolve_subtree_xml(tree_id, files):
    """A <SubTree ID>'s tree from this robot's synced files, for
    tree_builder's subtree_resolver: None when the robot has no such tree,
    so an empty tree ('') is told apart from a missing one (NODESYNC-35)."""
    if not tree_id:
        return None
    path = os.path.join(files["xparo_custom_behaviors_folder_path"], 'custom_aiml', f"{tree_id}.xml")
    if not os.path.isfile(path):
        return None
    return resolve_tree_xml(tree_id, files)


# Names the dashboard gives the frozen copies of a tree version marked for a
# stage (History -> Stages): ".xpv.<key>" (the tree) and ".xpv.<key>.<SubTree>"
# (each SubTree it runs, SubTree IDs already rewritten to these names). A
# task whose stage is marked names ".xpv.<key>" as its behaviour_tree_name.
# Dashboard tree names can never contain "." so they never collide.
HIDDEN_PREFIX = ".xpv."
HIDDEN_PRUNE_AGE_S = 600


def is_hidden(name):
    return isinstance(name, str) and name.startswith(HIDDEN_PREFIX)


def hidden_key(name):
    return name[len(HIDDEN_PREFIX):].split(".", 1)[0] if is_hidden(name) else ""


def _custom_aiml_dir(files):
    return os.path.join(files["xparo_custom_behaviors_folder_path"], 'custom_aiml')


# engine.py's custom_aiml branch writes a received tree as
# tree_file_content(text): a whole document as it is, anything else inside
# XPARO's own wrapper. The dashboard's tree_files hash is of `text` itself,
# so the check must undo exactly that wrapper. resolve_tree_xml's
# un-wrapping also strips, and un-wraps a one-tree document the dashboard
# stored as a document: hashing its output refused intact copies of trees
# that start with whitespace or are a one-tree <root> document.
_WRAP_HEAD, _WRAP_TAIL = tree_file_content("\x00").split("\x00")


def _read_hidden_raw(name, files):
    try:
        with open(os.path.join(_custom_aiml_dir(files), f"{name}.xml"), "r") as file:
            return file.read()
    except OSError:
        return None


def _received_text(raw):
    """The text the dashboard sent for a hidden file written as `raw`."""
    if raw.startswith(_WRAP_HEAD) and raw.endswith(_WRAP_TAIL) and len(raw) >= len(_WRAP_HEAD) + len(_WRAP_TAIL):
        inner = raw[len(_WRAP_HEAD):len(raw) - len(_WRAP_TAIL)]
        if not is_tree_document(inner):   # a document is never wrapped (tree_file_content)
            return inner
    return raw


def _received_hashes(raw):
    """Every hash the dashboard may have sent for a file written as `raw`
    (a stored document that happens to look exactly like the wrapper is
    written as it is, so both readings are accepted)."""
    return {sync_hash.content_hash(raw), sync_hash.content_hash(_received_text(raw))}


def hidden_file_hashes(files):
    """{hidden name: hash of what is on disk} for LOCAL_FILE_STATE's
    hidden_files: the dashboard re-sends any copy that is missing or
    different (they are never part of the file sync itself). The hash is
    of the text the dashboard sent, so an intact copy always matches."""
    folder = _custom_aiml_dir(files)
    out = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for filename in names:
        if filename.endswith('.xml') and is_hidden(filename[:-4]):
            name = filename[:-4]
            raw = _read_hidden_raw(name, files)
            if raw is not None:
                out[name] = sync_hash.content_hash(_received_text(raw))
    return out


def prune_hidden_files(files, tasks, now=None, min_age_s=HIDDEN_PRUNE_AGE_S):
    """Deletes hidden copies no synced task refers to any more, once they
    are older than min_age_s (a run started a moment before the new task
    list arrived may still be loading its SubTree files). Returns the names
    deleted."""
    referenced = set()
    for task in (tasks or {}).values():
        if not isinstance(task, dict):
            continue
        referenced.add(hidden_key(task.get("behaviour_tree_name")))
        for name in (task.get("tree_files") or {}):
            referenced.add(hidden_key(name))
    referenced.discard("")
    folder = _custom_aiml_dir(files)
    now = time.time() if now is None else now
    removed = []
    try:
        names = os.listdir(folder)
    except OSError:
        return removed
    for filename in names:
        name = filename[:-4] if filename.endswith('.xml') else ""
        if not is_hidden(name) or hidden_key(name) in referenced:
            continue
        path = os.path.join(folder, filename)
        try:
            if now - os.path.getmtime(path) < min_age_s:
                continue
            os.remove(path)
            removed.append(name)
        except OSError:
            continue
    return removed


def stage_label(stage):
    return {"production": "Production", "review": "Review", "development": "Development"}.get(stage, stage or "")


def load_verified_bundle(task, files):
    """A task whose stage runs a marked version carries tree_files {hidden
    name: hash}. Returns (tree_xml, {SubTree hidden name: xml}, problem):
    every file is read from disk and checked against its hash; a missing or
    changed file is refused (never replaced by a working copy on disk)."""
    expected = task.get("tree_files") or {}
    main = task.get("behaviour_tree_name", "")
    texts = {}
    bad = False
    for name, digest in expected.items():
        raw = _read_hidden_raw(name, files) if is_hidden(name) else None
        if raw is None or digest not in _received_hashes(raw):
            bad = True
            break
        # run it exactly as resolve_tree_xml would hand it over
        text = _extract_behavior_tree_fragment(raw)
        if not text:
            bad = True
            break
        texts[name] = text
    if bad or main not in texts:
        tree = task.get("tree_name") or "the main tree"
        stage = stage_label(task.get("tree_stage") or task.get("stage"))
        return "", {}, (f"This robot's copy of {tree}'s {stage} version is missing or was changed on the robot. "
                        f"It is fetched again when the robot reconnects; nothing was run.")
    return texts.pop(main), texts, ""


def tree_file_hash(behaviour_tree_name, files):
    """The hash of the tree this robot would load for `behaviour_tree_name`
    (same path rule and un-wrapping as resolve_tree_xml; the robot's own
    copy of the dashboard's sync_hash.content_hash, the hash the file sync
    compares), or "" if there is no such file. Task history then shows when
    a robot ran a copy that differs from the dashboard's."""
    tree_xml = resolve_tree_xml(behaviour_tree_name, files)
    return sync_hash.content_hash(tree_xml) if tree_xml else ""


def _parse_env_value(content, key):
    for line in (content or "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def _env_file_path(env_file, files):
    """A synced environment lives in custom_envs/custom_maps/<name>.env
    (engine.py's custom_maps branch); this looked in custom_envs/ itself,
    so every 'From env' input was empty on a robot-started run (ROSAPI-14).
    The old flat place is still read for files put there by hand."""
    if not env_file or os.path.basename(env_file) != env_file:
        return None
    root = files["xparo_custom_evns_folder_path"]
    for path in (os.path.join(root, "custom_maps", f"{env_file}.env"), os.path.join(root, f"{env_file}.env")):
        if os.path.exists(path):
            return path
    return None


def resolve_blackboard(blackboard_mapping, params, override_params, files, problems=None):
    """`problems` (a list), when given, gets one sentence per mapped input
    that couldn't be resolved -- the caller refuses the run rather than
    start it with that input silently missing."""
    override_params = override_params or {}
    params_by_id = {p.get("id"): p for p in (params or [])}
    resolved = {}
    for var_name, entry in (blackboard_mapping or {}).items():
        mapping_type = entry.get("mapping_type")
        if mapping_type == "default":
            resolved[var_name] = entry.get("value", "")
        elif mapping_type == "env":
            env_file, env_var = entry.get("env_file", ""), entry.get("env_var", "")
            env_path = _env_file_path(env_file, files)
            content = ""
            if env_path is not None:
                with open(env_path, "r") as file:
                    content = file.read()
            value = _parse_env_value(content, env_var)
            if value is not None:
                resolved[var_name] = value
            elif problems is not None:
                where = (f"{env_var!r} isn't set in it" if env_path is not None
                         else f"this robot has no environment {env_file!r} (custom_envs/custom_maps/{env_file}.env)")
                problems.append(f"input {var_name!r} comes from environment {env_file!r} / {env_var!r}, but {where}")
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


_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def names_the_tree_reads(tree_xml, files, missing_subtrees=None):
    """Every name the tree could read from the blackboard: each word in its
    port values, Script code and conditions ("{dock_name}", "n := dock_name",
    _skipIf="!dock_name", entry="dock_name"), the robot's synced trees its
    SubTrees name included. Generous on purpose -- an extra name only keeps
    a mapping. None when the tree can't be read (every mapping is kept; the
    run then fails on the tree itself). A SubTree this robot doesn't have
    is added to missing_subtrees (a list) when one is given."""
    names, loaded, pending = set(), set(), [(None, tree_xml)]
    while pending:
        source_id, source = pending.pop()
        try:
            root = parse_fragment(source)
        except TreeParseError:
            if loaded and not (source or "").strip():
                if missing_subtrees is not None:
                    missing_subtrees.append(source_id)
                continue  # a SubTree this robot doesn't have: the run says so
            return None
        defined = {tree.get("ID") for tree in root.iter("BehaviorTree")}
        models = {el for model in root.iter("TreeNodesModel") for el in model.iter()}
        for el in root.iter():
            if el in models:
                continue  # Groot2's port declarations, not reads
            for key, value in el.attrib.items():
                if key not in ("ID", "name"):
                    names.update(_NAME_RE.findall(value))
            tree_id = el.get("ID") if el.tag == "SubTree" else None
            if tree_id and tree_id not in defined and tree_id not in loaded:
                loaded.add(tree_id)
                pending.append((tree_id, resolve_tree_xml(tree_id, files)))
    return names


def mappings_the_tree_reads(blackboard_mapping, tree_xml, files):
    """(the mappings whose input the tree reads, sorted names of the rest).
    A mapping outlives its input when the tree is edited -- the dashboard
    stops showing it, the synced task keeps it -- and a stale From-env one
    refused every run once its environment was gone (ROSAPI-17)."""
    blackboard_mapping = blackboard_mapping or {}
    missing_subtrees = []
    reads = names_the_tree_reads(tree_xml, files, missing_subtrees)
    if reads is None:
        return blackboard_mapping, []
    used = {name: entry for name, entry in blackboard_mapping.items() if name in reads}
    if missing_subtrees:
        # The missing SubTree may read the rest, so they aren't called
        # unread (R3 review); the run stops at it (invalid_tree names it).
        return used, []
    return used, sorted(name for name in blackboard_mapping if name not in reads)


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
    subtrees, bundle_problem = None, ""
    if task.get("tree_files"):
        # a marked version: its frozen files only, each checked
        tree_xml, subtrees, bundle_problem = load_verified_bundle(task, files)
    else:
        tree_xml = resolve_tree_xml(task.get("behaviour_tree_name", ""), files)
    mapping, ignored_inputs = mappings_the_tree_reads(task.get("blackboard_mapping"), tree_xml, files)
    input_problems = []
    blackboard = resolve_blackboard(mapping, task.get("params"), override_params, files, input_problems)
    # Overrides are read by param name (resolve_blackboard); one naming no
    # param of the task reaches nothing, and the run said nothing about it
    # (KUNAL-25) -- handle_run_task now names it in the result.
    param_names = {param.get("name") for param in (task.get("params") or [])}
    val = {
        "task_id": task_id,
        "tree_xml": tree_xml,
        "blackboard": blackboard,
        "input_problems": input_problems,
        "ignored_inputs": ignored_inputs,
        # Names cut to IGNORED_OVERRIDE_NAME_CHARS: they go into the result and
        # task history, and only values are size-checked (validate_run_task_request).
        "ignored_overrides": sorted(str(name)[:IGNORED_OVERRIDE_NAME_CHARS] for name in (override_params or {})
                                    if name not in param_names),
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
        # Newer dashboards also sync these; older caches simply lack them
        # (handle_run_task then uses its default time limit).
        "timeout_s": task.get("timeout"),
        "task_title": task.get("title") or "",
        # the file it runs (behaviour_tree_name may be a marked version's
        # hidden copy); older caches only have behaviour_tree_name
        "tree_name": task.get("tree_name", task.get("behaviour_tree_name", "")),
        "trigger": "ros_topic",
        # Stage versions (History -> Stages on the dashboard): which saved
        # version this is ('' = the working copy) and the hash of what was
        # loaded. A task whose tree no longer exists, or whose marked copy
        # is missing/changed here, carries tree_error (handle_run_task
        # reports it as invalid_tree). Older dashboards send none of them.
        "tree_stage": task.get("tree_stage") or "",
        "tree_version": task.get("tree_version") or "",
        "tree_version_at": task.get("tree_version_at") or "",
        "tree_error": bundle_problem or task.get("tree_error") or "",
        "tree_hash": task.get("tree_hash") or (sync_hash.content_hash(tree_xml) if tree_xml else ""),
    }
    if subtrees is not None:
        # the bundle's SubTrees only: no SubTree is read from disk for it
        val["subtrees"] = subtrees
        val["verified_bundle"] = True
    if bundle_problem:
        val["resync_files"] = True
    return val


# Anything on the ROS domain can publish /xparo/run_task (default
# ROS_DOMAIN_ID 0 reaches the whole site LAN), so a request is checked
# before anything touches it: a list where a dict was expected used to
# raise inside resolve_blackboard and take the whole node down (ROSAPI-6),
# and a 2 MB override value ran and was stored twice in the task's history
# report (ROSAPI-10). Task params are short text (a goal name, a number).
MAX_OVERRIDE_PARAMS = 50
MAX_OVERRIDE_VALUE_CHARS = 10_000
IGNORED_OVERRIDE_NAME_CHARS = 100
_EXPECTED_SHAPE = 'expected JSON {"task_id": "<task id>", "override_params": {"name": "value"}} or a bare task id'


def parse_run_task_message(data):
    """A /xparo/run_task std_msgs/String payload -> (payload dict, problem).
    Problem is "" when the message is usable. JSON {"task_id": ...} as
    documented; a bare task id (what the docs prose suggests) is accepted
    too."""
    text = (data or "").strip()
    if text and text[0] not in '{["' and " " not in text:
        return {"task_id": text}, ""
    try:
        payload = json.loads(text)
    except ValueError as e:
        return None, f"could not parse the message as JSON ({e}) -- {_EXPECTED_SHAPE}"
    if not isinstance(payload, dict):
        return None, f"the message is JSON but not an object -- {_EXPECTED_SHAPE}"
    for key in ("robot", "device_id"):
        # A list here was read as "another robot" and the request was
        # dropped with no answer at all (ROSAPI-13).
        if payload.get(key) is not None and not isinstance(payload[key], str):
            return payload, f'"{key}" must be a string (a device id or hostname), not {type(payload[key]).__name__}'
    if "allow_parallel" in payload and not isinstance(payload["allow_parallel"], bool):
        return payload, f'"allow_parallel" must be true or false, not {json.dumps(payload["allow_parallel"], default=str)}'
    return payload, ""


def validate_run_task_request(task_id, override_params):
    """Why a parsed /xparo/run_task request can't be served, or "" when it
    can -- the caller answers invalid_request on /xparo/task_result."""
    if task_id is not None and not isinstance(task_id, str):
        return f'"task_id" must be a string (the task\'s id), not {type(task_id).__name__} -- {_EXPECTED_SHAPE}'
    if not isinstance(task_id, str) or not task_id.strip():
        return f'missing "task_id" -- {_EXPECTED_SHAPE}'
    if override_params is None:
        return ""
    if not isinstance(override_params, dict):
        return f'"override_params" must be a JSON object of name: value pairs, not {type(override_params).__name__}'
    if len(override_params) > MAX_OVERRIDE_PARAMS:
        return f'"override_params" has {len(override_params)} entries; at most {MAX_OVERRIDE_PARAMS} are allowed'
    for name, value in override_params.items():
        size = len(value) if isinstance(value, str) else len(json.dumps(value, default=str))
        if size > MAX_OVERRIDE_VALUE_CHARS:
            return (f'override_params[{name!r}] is {size} characters; at most {MAX_OVERRIDE_VALUE_CHARS} are allowed '
                    f'(the value is stored in the task history)')
    return ""
