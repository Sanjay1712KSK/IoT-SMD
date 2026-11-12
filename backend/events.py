"""Event history stored in SQLite (survives restarts) + snapshot housekeeping."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,          -- unix time (seconds)
    kind      TEXT    NOT NULL,          -- known | unknown | noface | error | system
    name      TEXT,
    message   TEXT    NOT NULL,
    distance  REAL,
    snapshot  TEXT                       -- file name inside snapshots/, or NULL
)
"""


class EventStore:
    def __init__(self, db_path: Path) -> None:
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        # One shared connection guarded by a lock: simple and fine for a few writes per minute.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(SCHEMA)
            self._conn.commit()

    def add(self, kind: str, message: str, name: str | None = None,
            distance: float | None = None, snapshot: str | None = None) -> dict:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, kind, name, message, distance, snapshot) VALUES (?,?,?,?,?,?)",
                (ts, kind, name, message, distance, snapshot))
            self._conn.commit()
            event_id = cur.lastrowid
        return {"id": event_id, "ts": ts, "kind": kind, "name": name,
                "message": message, "distance": distance, "snapshot": snapshot}

    def since(self, after_id: int = 0, limit: int = 50) -> list[dict]:
        """Events with id > after_id (oldest first). after_id=0 -> the most recent `limit` events."""
        limit = max(1, min(limit, 200))
        with self._lock:
            if after_id <= 0:
                rows = self._conn.execute(
                    "SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [dict(r) for r in rows]


def cleanup_snapshots(folder: Path, max_age_days: float) -> int:
    """Delete snapshot images older than max_age_days. Returns how many were deleted."""
    if max_age_days <= 0 or not folder.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for f in folder.glob("*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            pass
    if deleted:
        log.info("Deleted %d snapshots older than %s days", deleted, max_age_days)
    return deleted
