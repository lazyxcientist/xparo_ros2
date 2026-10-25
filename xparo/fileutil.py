"""Crash-safe file writes.

open(path, 'w') empties the file first: a power cut or a full disk in the
middle left tasks.json, manifests and pointers empty or half-written, and
on the next boot every task was "unknown". These write a temporary file
next to the target and swap it in with os.replace, so the old content stays
intact until the new content is completely on disk.
"""
import json
import os
import tempfile


def atomic_write(path, text, encoding="utf-8"):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, data, **dump_kwargs):
    atomic_write(path, json.dumps(data, **dump_kwargs))
