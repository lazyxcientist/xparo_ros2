"""Every play, on disk, until the server has it.

One row per play (completed / dismissed / interrupted / error). Rows are
uploaded in batches (ADS_PLAYS) with their own id as `local_id`; the
server's ads_plays_ack says which ids it stored (uploaded=1) or refused
(uploaded=2). Unacknowledged rows are sent again later -- the server
recognises duplicates -- so nothing is lost when the robot is offline.

A play may carry a screenshot taken while it ran (proof_path). It is sent
on its own (ADS_PROOF) once the play itself is stored, and the file is
deleted when the server answers ads_proof_ack. proof_state: 0 waiting,
1 sent (resent after RESEND_AFTER_SEC without an answer), 2 done."""
import os
import sqlite3
import threading
import time

COLUMNS = ("ad_id", "title", "media_type", "placement", "status", "play_started_at",
           "play_ended_at", "duration_played_sec", "error_message")
PROOF_COLUMNS = (("proof_path", "TEXT"), ("proof_captured_at", "TEXT"), ("proof_state", "INTEGER DEFAULT 0"),
                 ("proof_sent_at", "REAL"))
RESEND_AFTER_SEC = 120


class PlayLog:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plays ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id TEXT, title TEXT, media_type TEXT, placement TEXT,"
                " status TEXT, play_started_at TEXT, play_ended_at TEXT, duration_played_sec REAL,"
                " error_message TEXT, uploaded INTEGER DEFAULT 0, sent_at REAL)")
            have = {row[1] for row in conn.execute("PRAGMA table_info(plays)")}
            for name, kind in PROOF_COLUMNS:  # logs written before screenshots existed
                if name not in have:
                    conn.execute(f"ALTER TABLE plays ADD COLUMN {name} {kind}")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, record):
        columns = COLUMNS + ("proof_path", "proof_captured_at")
        values = [record.get(c) for c in columns]
        with self._lock, self._connect() as conn:
            cur = conn.execute(f"INSERT INTO plays ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                               values)
            return cur.lastrowid

    def due_for_upload(self, limit=500, now=None):
        """Rows the server hasn't acknowledged and that weren't sent in the
        last RESEND_AFTER_SEC seconds. Marks them as sent now."""
        now = now or time.time()
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM plays WHERE uploaded = 0 AND (sent_at IS NULL OR sent_at < ?) ORDER BY id LIMIT ?",
                (now - RESEND_AFTER_SEC, limit)).fetchall()
            if rows:
                conn.executemany("UPDATE plays SET sent_at = ? WHERE id = ?", [(now, r["id"]) for r in rows])
        return [dict({c: r[c] for c in COLUMNS}, local_id=r["id"]) for r in rows]

    def acknowledge(self, stored_ids=(), rejected_ids=()):
        with self._lock, self._connect() as conn:
            conn.executemany("UPDATE plays SET uploaded = 1 WHERE id = ?", [(i,) for i in stored_ids])
            conn.executemany("UPDATE plays SET uploaded = 2 WHERE id = ?", [(i,) for i in rejected_ids])
            # A refused play's screenshot has nothing to belong to on the server.
            refused = conn.execute(
                f"SELECT id, proof_path FROM plays WHERE id IN ({', '.join('?' * len(rejected_ids))}) AND proof_path IS NOT NULL",
                list(rejected_ids)).fetchall() if rejected_ids else []
            conn.executemany("UPDATE plays SET proof_state = 2 WHERE id = ?", [(i,) for i, _ in refused])
        for _, path in refused:
            _remove(path)

    def proofs_due(self, limit=5, now=None):
        """Screenshots of stored plays that haven't been sent (or got no answer). Marks them sent now."""
        now = now or time.time()
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, ad_id, play_started_at, proof_path, proof_captured_at FROM plays"
                " WHERE uploaded = 1 AND proof_path IS NOT NULL AND (proof_state = 0 OR (proof_state = 1 AND"
                " (proof_sent_at IS NULL OR proof_sent_at < ?))) ORDER BY id LIMIT ?",
                (now - RESEND_AFTER_SEC, limit)).fetchall()
            if rows:
                conn.executemany("UPDATE plays SET proof_state = 1, proof_sent_at = ? WHERE id = ?",
                                 [(now, r["id"]) for r in rows])
        return [dict(r) for r in rows]

    def proof_done(self, local_id):
        """The server answered for this screenshot (kept or refused): delete the file."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT proof_path FROM plays WHERE id = ?", (local_id,)).fetchone()
            conn.execute("UPDATE plays SET proof_state = 2 WHERE id = ?", (local_id,))
        if row and row[0]:
            _remove(row[0])

    def proof_unsent(self, local_id):
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE plays SET proof_state = 0, proof_sent_at = NULL WHERE id = ?", (local_id,))

    def mark_unsent(self):
        """After a reconnect: send whatever is still unacknowledged right away."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE plays SET sent_at = NULL WHERE uploaded = 0")
            conn.execute("UPDATE plays SET proof_state = 0, proof_sent_at = NULL WHERE proof_state = 1")

    def played_since(self, ad_id, since_iso):
        """Has this ad a logged play that started at or after `since_iso` (local ISO time)?"""
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM plays WHERE ad_id = ? AND play_started_at >= ? LIMIT 1",
                                (ad_id, str(since_iso)[:19])).fetchone() is not None

    def counts(self):
        with self._connect() as conn:
            return dict(conn.execute("SELECT uploaded, COUNT(*) FROM plays GROUP BY uploaded").fetchall())

    def recent(self, limit=50):
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM plays ORDER BY id DESC LIMIT ?", (limit,))]


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
