"""SQLite-backed metadata for published index versions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from mimir.domain.errors import StorageError
from mimir.domain.index_state import IndexVersion, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_versions (
    version      TEXT PRIMARY KEY,
    graph_path   TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 0,
    node_count   INTEGER NOT NULL DEFAULT 0,
    edge_count   INTEGER NOT NULL DEFAULT 0,
    repo_commits TEXT NOT NULL DEFAULT '{}',
    schema_version INTEGER NOT NULL DEFAULT 1,
    embedding_model TEXT,
    config_hash TEXT,
    embedding_dim INTEGER,
    created_by TEXT,
    created_at   TEXT NOT NULL,
    activated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_index_versions_active ON index_versions(active);
CREATE INDEX IF NOT EXISTS idx_index_versions_created ON index_versions(created_at);
"""


class SqliteIndexMetadataStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._ensure_columns()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to initialise index metadata store: {exc}") from exc

    def _ensure_columns(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(index_versions)")
        existing = {row[1] for row in cur.fetchall()}
        for col, defn in [
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
            ("embedding_model", "TEXT"),
            ("embedding_dim", "INTEGER"),
            ("config_hash", "TEXT"),
            ("created_by", "TEXT"),
        ]:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE index_versions ADD COLUMN {col} {defn}")
        self._conn.commit()

    def publish(self, version: IndexVersion, *, activate: bool = True) -> None:
        try:
            active = 1 if activate else int(version.active)
            activated_at = utc_now_iso() if activate else version.activated_at
            cur = self._conn.cursor()
            if activate:
                cur.execute("UPDATE index_versions SET active = 0")
            cur.execute(
                "INSERT OR REPLACE INTO index_versions "
                "(version, graph_path, active, node_count, edge_count, repo_commits, "
                "schema_version, embedding_model, embedding_dim, config_hash, created_by, "
                "created_at, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version.version,
                    version.graph_path,
                    active,
                    version.node_count,
                    version.edge_count,
                    json.dumps(version.repo_commits),
                    version.schema_version,
                    version.embedding_model,
                    version.embedding_dim,
                    version.config_hash,
                    version.created_by,
                    version.created_at,
                    activated_at,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise StorageError(f"Failed to publish index version: {exc}") from exc

    def get_active(self) -> Optional[IndexVersion]:
        try:
            cur = self._conn.execute(
                "SELECT version, graph_path, active, node_count, edge_count, repo_commits, "
                "schema_version, embedding_model, embedding_dim, config_hash, created_by, "
                "created_at, activated_at "
                "FROM index_versions WHERE active = 1 ORDER BY activated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            return self._row_to_version(row) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to read active index version: {exc}") from exc

    def get(self, version: str) -> Optional[IndexVersion]:
        try:
            cur = self._conn.execute(
                "SELECT version, graph_path, active, node_count, edge_count, repo_commits, "
                "schema_version, embedding_model, embedding_dim, config_hash, created_by, "
                "created_at, activated_at "
                "FROM index_versions WHERE version = ?",
                (version,),
            )
            row = cur.fetchone()
            return self._row_to_version(row) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to read index version: {exc}") from exc

    def list_versions(self, limit: int = 20) -> list[IndexVersion]:
        try:
            cur = self._conn.execute(
                "SELECT version, graph_path, active, node_count, edge_count, repo_commits, "
                "schema_version, embedding_model, embedding_dim, config_hash, created_by, "
                "created_at, activated_at "
                "FROM index_versions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [self._row_to_version(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to list index versions: {exc}") from exc

    def activate(self, version: str) -> Optional[IndexVersion]:
        try:
            existing = self.get(version)
            if existing is None:
                return None
            activated_at = utc_now_iso()
            cur = self._conn.cursor()
            cur.execute("UPDATE index_versions SET active = 0")
            cur.execute(
                "UPDATE index_versions SET active = 1, activated_at = ? WHERE version = ?",
                (activated_at, version),
            )
            self._conn.commit()
            return self.get(version)
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise StorageError(f"Failed to activate index version: {exc}") from exc

    def delete(self, version: str) -> None:
        try:
            self._conn.execute("DELETE FROM index_versions WHERE version = ?", (version,))
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise StorageError(f"Failed to delete index version: {exc}") from exc

    @staticmethod
    def _row_to_version(row) -> IndexVersion:
        return IndexVersion(
            version=row[0],
            graph_path=row[1],
            active=bool(row[2]),
            node_count=row[3],
            edge_count=row[4],
            repo_commits=json.loads(row[5] or "{}"),
            schema_version=row[6],
            embedding_model=row[7],
            embedding_dim=row[8],
            config_hash=row[9],
            created_by=row[10],
            created_at=row[11],
            activated_at=row[12],
        )

    def close(self) -> None:
        self._conn.close()
