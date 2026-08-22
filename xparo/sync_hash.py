"""Bidirectional file sync: content hashing shared by the conflict-
detection logic on both sides of the Django <-> robot sync boundary.

Deliberately duplicated (not imported) from the outer Django repo's
apps/analytics/sync_hash.py -- these are two separate git repos with no
shared import path between them, so the two copies are kept in lockstep
by convention rather than by a shared dependency. Keep any change to
normalize_for_hash/content_hash mirrored in both files.
"""
import hashlib


def normalize_for_hash(text):
    """CRLF/CR -> LF, trailing whitespace per line stripped, exactly one
    trailing newline -- so an editor's line-ending or trim-on-save
    auto-formatting can never manufacture a false-positive sync conflict.
    """
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    normalized = '\n'.join(line.rstrip(' \t') for line in normalized.split('\n'))
    return normalized.rstrip('\n') + '\n'


def content_hash(*parts):
    """Hashes one or more text parts together (e.g. a C++ file's source +
    header_source) -- NUL-joined, since NUL is never legal inside any of
    these text formats, so a different split of the same concatenated text
    can't collide with this one.
    """
    joined = '\x00'.join(normalize_for_hash(p) for p in parts)
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()
