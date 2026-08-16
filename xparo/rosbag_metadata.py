"""Reads a completed bag session's metadata.yaml (written by rosbag2's
recorder on a clean Stop()) and turns it into what blackbox_manager.py
uploads alongside the bag data file: a compact summary (duration, message
count, topic count, busiest topics -- everything the Sensors Database
popup needs for a short, readable analysis) plus the raw YAML text (for
that popup's "Raw YAML" toggle). Previously nothing read metadata.yaml at
all -- _process_uploads() sent a hardcoded 'data': '{}' regardless of what
was actually on disk, which is why every rosbag row's "Data (preview)" was
empty no matter how a session ended.

Force-exit handling: if the recorder process was killed (SIGKILL, OOM,
crash) before Stop() could run, metadata.yaml is genuinely never written --
confirmed live by killing a real `ros2 bag record` process mid-session:
rosbag2's mcap writer buffers chunks in memory and only finalizes/writes
metadata on a clean stop, so this is inherent to the recorder, not a bug in
this file. `ros2 bag reindex` (a real rosbag2 CLI tool) is attempted first
since it can recover metadata from a bag with complete-but-unindexed chunks
(a session killed shortly after starting has no flushed chunks yet and
reindex can't help there either -- also confirmed live). If reindex can't
produce a metadata.yaml either, the bag file itself is still uploaded (it
may still contain real, playable data) with a status explicitly marking
metadata as unavailable, rather than silently reporting an empty summary
indistinguishable from "this bag genuinely has no data".
"""
import os
import subprocess

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with every ROS2 install
    yaml = None


def try_reindex(bag_dir, storage_identifier='mcap', timeout=30):
    """Best-effort, never raises. Returns True if metadata.yaml exists in
    bag_dir after this call (whether it already did, or reindex just
    produced one)."""
    meta_path = os.path.join(bag_dir, 'metadata.yaml')
    if os.path.exists(meta_path):
        return True
    try:
        subprocess.run(
            ['ros2', 'bag', 'reindex', bag_dir, '-s', storage_identifier],
            capture_output=True, timeout=timeout, check=False,
        )
    except Exception as e:
        print(f"[rosbag_metadata] reindex failed for {bag_dir}: {e}")
    return os.path.exists(meta_path)


def summarize_metadata(parsed_yaml):
    """parsed_yaml is whatever yaml.safe_load produces from a real
    metadata.yaml. Strips the QoS-profile noise (deadline/lifespan/
    liveliness nanosecond blocks -- the overwhelming majority of the raw
    file's size) down to what's actually useful for a short analysis:
    duration, message/topic counts, and a busiest-first topic list. Valid
    YAML doesn't have to be a mapping (e.g. a bare scalar string still
    parses successfully) -- guarded here so a malformed-but-parseable
    metadata.yaml degrades to an empty summary instead of an AttributeError."""
    if not isinstance(parsed_yaml, dict):
        parsed_yaml = {}
    info = parsed_yaml.get('rosbag2_bagfile_information') or {}
    if not isinstance(info, dict):
        info = {}
    topics = []
    for entry in info.get('topics_with_message_count') or []:
        meta = entry.get('topic_metadata') or {}
        topics.append({
            'name': meta.get('name', ''),
            'type': meta.get('type', ''),
            'message_count': entry.get('message_count', 0) or 0,
        })
    topics.sort(key=lambda t: t['message_count'], reverse=True)

    duration_ns = ((info.get('duration') or {}).get('nanoseconds')) or 0
    starting_ns = ((info.get('starting_time') or {}).get('nanoseconds_since_epoch')) or 0

    return {
        'storage_identifier': info.get('storage_identifier', ''),
        'ros_distro': info.get('ros_distro', ''),
        'compression_format': info.get('compression_format', ''),
        'compression_mode': info.get('compression_mode', ''),
        'duration_seconds': round(duration_ns / 1e9, 2),
        'message_count': info.get('message_count', 0) or 0,
        'topic_count': len(topics),
        'starting_time_unix': (starting_ns / 1e9) if starting_ns else None,
        'topics': topics,
    }


def build_sensor_data(bag_data_file_path):
    """The full 'data' payload blackbox_manager.py uploads for one bag
    file. Always returns a dict, never raises. `metadata_status` is one of:
      'ok'          -- metadata.yaml was read successfully
      'reindexed'   -- metadata.yaml didn't exist but ros2 bag reindex built one
      'unavailable' -- no metadata could be found or produced (the recorder
                       most likely didn't shut down cleanly for this
                       session -- see this module's docstring)
      'unparseable' -- metadata.yaml exists but wasn't valid YAML
    """
    bag_dir = os.path.dirname(bag_data_file_path)
    meta_path = os.path.join(bag_dir, 'metadata.yaml')

    status = 'ok'
    if not os.path.exists(meta_path):
        if try_reindex(bag_dir):
            status = 'reindexed'
        else:
            return {'metadata_status': 'unavailable'}

    if yaml is None:
        return {'metadata_status': 'unavailable'}

    try:
        with open(meta_path, 'r') as f:
            raw_yaml_text = f.read()
        parsed = yaml.safe_load(raw_yaml_text)
        summary = summarize_metadata(parsed)
    except Exception as e:
        print(f"[rosbag_metadata] failed to read/parse {meta_path}: {e}")
        return {'metadata_status': 'unparseable'}

    return {
        'metadata_status': status,
        'summary': summary,
        'raw_yaml': raw_yaml_text,
    }
