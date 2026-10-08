"""Hermes-independent inbox ledger: process each post or comment ID once."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_OPEN = ("queued", "liked")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Claim:
    source_id: str
    feed_element_id: str
    kind: str
    requester_id: str


class Inbox:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._db = sqlite3.connect(str(path))
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS mentions (
              source_id TEXT PRIMARY KEY,
              feed_element_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              requester_id TEXT NOT NULL,
              status TEXT NOT NULL,
              claimed_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self._db.commit()
        os.chmod(path, 0o600)

    def _now(self) -> str:
        return self._clock().isoformat()

    def _get_state(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    def scan_since(self, max_catchup: timedelta, *, cursor: str = "scan_since") -> datetime:
        """Return the scan start; first use stores now to skip pre-start posts."""
        now = self._clock()
        stored = self._get_state(cursor)
        if stored is None:
            self._set_state(cursor, now.isoformat())
            return now
        return max(datetime.fromisoformat(stored), now - max_catchup)

    def advance_scan(self, started_at: datetime, overlap: timedelta, *, cursor: str = "scan_since") -> None:
        candidate = started_at - overlap
        stored = self._get_state(cursor)
        if stored is not None and datetime.fromisoformat(stored) >= candidate:
            return
        self._set_state(cursor, candidate.isoformat())

    def claim(self, c: Claim) -> bool:
        now = self._now()
        cur = self._db.execute(
            "INSERT OR IGNORE INTO mentions VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (c.source_id, c.feed_element_id, c.kind, c.requester_id, now, now),
        )
        self._db.commit()
        return cur.rowcount == 1

    def set_status(self, source_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE mentions SET status = ?, updated_at = ? WHERE source_id = ?",
            (status, self._now(), source_id),
        )
        self._db.commit()

    def answered_in_last_hour(self) -> int:
        since = (self._clock() - timedelta(hours=1)).isoformat()
        row = self._db.execute(
            "SELECT COUNT(*) FROM mentions WHERE status = 'answered' AND updated_at > ?", (since,)
        ).fetchone()
        return int(row[0])

    def pending_requester(self, feed_element_id: str) -> str | None:
        row = self._db.execute(
            "SELECT requester_id FROM mentions WHERE feed_element_id = ? AND status IN (?, ?) "
            "ORDER BY claimed_at DESC LIMIT 1",
            (feed_element_id, *_OPEN),
        ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._db.close()
