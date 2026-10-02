"""Every play, on disk, until the server has it.

One row per play (completed / dismissed / interrupted / error). Rows are
uploaded in batches (ADS_PLAYS) with their own id as `local_id`; the
server's ads_plays_ack says which ids it stored (uploaded=1) or refused
(uploaded=2). Unacknowledged rows are sent again later -- the server
recognises duplicates -- so nothing is lost when the robot is offline."""
import sqlite3
import threading
import time

COLUMNS = ("ad_id", "title", "media_type", "placement", "status", "play_started_at",
           "play_ended_at", "duration_played_sec", "error_message")
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

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, record):
        values = [record.get(c) for c in COLUMNS]
        with self._lock, self._connect() as conn:
            cur = conn.execute(f"INSERT INTO plays ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
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

    def mark_unsent(self):
        """After a reconnect: send whatever is still unacknowledged right away."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE plays SET sent_at = NULL WHERE uploaded = 0")

    def counts(self):
        with self._connect() as conn:
            return dict(conn.execute("SELECT uploaded, COUNT(*) FROM plays GROUP BY uploaded").fetchall())

    def recent(self, limit=50):
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM plays ORDER BY id DESC LIMIT ?", (limit,))]
