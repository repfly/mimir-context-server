"""SQLite-backed session persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from mimir.domain.errors import StorageError
from mimir.domain.session import Session

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""

#: Sessions not updated in this many days are automatically purged.
_EXPIRY_DAYS = 7


class SqliteSessionStore:
    """SQLite persistence for session state."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._purge_expired()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to init session store: {exc}") from exc

    def _purge_expired(self) -> None:
        """Remove sessions that haven't been updated within the expiry window."""
        try:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE updated_at < datetime('now', ?)",
                (f"-{_EXPIRY_DAYS} days",),
            )
            if cur.rowcount > 0:
                self._conn.commit()
                logger.info("Purged %d expired sessions (older than %d days)", cur.rowcount, _EXPIRY_DAYS)
        except sqlite3.Error:
            pass  # non-critical, best-effort cleanup

    def save(self, session: Session) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, data, updated_at) "
                "VALUES (?, ?, datetime('now'))",
                (session.session_id, json.dumps(session.to_dict())),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to save session: {exc}") from exc

    def load(self, session_id: str) -> Optional[Session]:
        try:
            cur = self._conn.execute(
                "SELECT data FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            return Session.from_dict(json.loads(row[0])) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to load session: {exc}") from exc

    def list_sessions(self) -> list[str]:
        try:
            cur = self._conn.execute("SELECT session_id FROM sessions ORDER BY updated_at DESC")
            return [r[0] for r in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to list sessions: {exc}") from exc

    def delete(self, session_id: str) -> None:
        try:
            self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to delete session: {exc}") from exc

    def clear(self) -> None:
        """Delete all sessions."""
        try:
            self._conn.execute("DELETE FROM sessions")
            self._conn.commit()
            logger.info("Session store cleared")
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to clear session store: {exc}") from exc

    def close(self) -> None:
        self._conn.close()
