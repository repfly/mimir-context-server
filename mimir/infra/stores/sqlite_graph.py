"""SQLite-backed graph persistence.

Schema uses two tables — ``nodes`` and ``edges`` — plus a ``repo_state``
table that tracks last-indexed commit per repository for incremental updates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
from pathlib import Path
from typing import Optional

from mimir.domain.errors import StorageError
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    path        TEXT,
    start_line  INTEGER,
    end_line    INTEGER,
    summary     TEXT,
    raw_code    TEXT,
    signature   TEXT,
    docstring   TEXT,
    last_modified       TEXT,
    modification_count  INTEGER DEFAULT 0,
    last_retrieved      TEXT,
    retrieval_count     INTEGER DEFAULT 0,
    co_retrieved_with   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edges (
    source   TEXT NOT NULL,
    target   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    weight   REAL DEFAULT 1.0,
    metadata TEXT DEFAULT '{}',
    PRIMARY KEY (source, target, kind)
);

CREATE TABLE IF NOT EXISTS repo_state (
    repo_name   TEXT PRIMARY KEY,
    commit_hash TEXT NOT NULL,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS embeddings (
    node_id    TEXT PRIMARY KEY,
    vector     BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_repo ON nodes(repo);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
"""


class SqliteGraphStore:
    """SQLite persistence for the unified code graph."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to initialise graph store at {db_path}: {exc}") from exc
        logger.info("Graph store initialised at %s", db_path)

    def save(self, graph: CodeGraph) -> None:
        """Persist the entire graph (full overwrite)."""
        try:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM nodes")
            cur.execute("DELETE FROM edges")
            cur.execute("DELETE FROM embeddings")

            node_rows = [
                (
                    n.id, n.repo, n.kind.value, n.name, n.path,
                    n.start_line, n.end_line, n.summary, n.raw_code,
                    n.signature, n.docstring,
                    n.last_modified, n.modification_count,
                    n.last_retrieved, n.retrieval_count,
                    json.dumps(n.co_retrieved_with),
                )
                for n in graph.all_nodes()
            ]
            cur.executemany(
                "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                node_rows,
            )

            edge_rows = [
                (e.source, e.target, e.kind.value, e.weight, json.dumps(e.metadata))
                for e in graph.all_edges()
            ]
            cur.executemany(
                "INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?)",
                edge_rows,
            )

            # Save embeddings as compact float32 blobs
            emb_rows = []
            for n in graph.all_nodes():
                if n.embedding:
                    blob = struct.pack(f'{len(n.embedding)}f', *n.embedding)
                    emb_rows.append((n.id, blob))
            if emb_rows:
                cur.executemany(
                    "INSERT OR REPLACE INTO embeddings (node_id, vector) VALUES (?, ?)",
                    emb_rows,
                )

            self._conn.commit()
            logger.info(
                "Saved graph: %d nodes, %d edges, %d embeddings",
                len(node_rows), len(edge_rows), len(emb_rows),
            )
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise StorageError(f"Failed to save graph: {exc}") from exc

    def save_partial(self, nodes: list, edges: list) -> None:
        """Insert or replace specific nodes and edges (no full wipe).

        Used by incremental indexing to persist only the delta.
        *nodes* and *edges* are domain ``Node`` / ``Edge`` objects.
        """
        try:
            cur = self._conn.cursor()

            node_rows = [
                (
                    n.id, n.repo, n.kind.value, n.name, n.path,
                    n.start_line, n.end_line, n.summary, n.raw_code,
                    n.signature, n.docstring,
                    n.last_modified, n.modification_count,
                    n.last_retrieved, n.retrieval_count,
                    json.dumps(n.co_retrieved_with),
                )
                for n in nodes
            ]
            cur.executemany(
                "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                node_rows,
            )

            edge_rows = [
                (e.source, e.target, e.kind.value, e.weight, json.dumps(e.metadata))
                for e in edges
            ]
            cur.executemany(
                "INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?)",
                edge_rows,
            )

            # Embeddings
            emb_rows = []
            for n in nodes:
                if n.embedding:
                    blob = struct.pack(f'{len(n.embedding)}f', *n.embedding)
                    emb_rows.append((n.id, blob))
            if emb_rows:
                cur.executemany(
                    "INSERT OR REPLACE INTO embeddings (node_id, vector) VALUES (?, ?)",
                    emb_rows,
                )

            self._conn.commit()
            logger.info(
                "Saved partial: %d nodes, %d edges, %d embeddings",
                len(node_rows), len(edge_rows), len(emb_rows),
            )
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise StorageError(f"Failed to save partial graph: {exc}") from exc

    def load(self) -> CodeGraph:
        """Load the full graph from storage."""
        graph = CodeGraph()
        try:
            cur = self._conn.cursor()

            # Load nodes
            cur.execute("SELECT * FROM nodes")
            columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                data = dict(zip(columns, row))
                data["kind"] = NodeKind(data["kind"])
                co_ret = data.pop("co_retrieved_with", "{}")
                node = Node(
                    id=data["id"],
                    repo=data["repo"],
                    kind=data["kind"],
                    name=data["name"],
                    path=data.get("path"),
                    start_line=data.get("start_line"),
                    end_line=data.get("end_line"),
                    summary=data.get("summary"),
                    raw_code=data.get("raw_code"),
                    signature=data.get("signature"),
                    docstring=data.get("docstring"),
                    last_modified=data.get("last_modified"),
                    modification_count=data.get("modification_count", 0),
                    last_retrieved=data.get("last_retrieved"),
                    retrieval_count=data.get("retrieval_count", 0),
                    co_retrieved_with=json.loads(co_ret) if co_ret else {},
                )
                graph.add_node(node)

            # Load edges
            cur.execute("SELECT * FROM edges")
            for row in cur.fetchall():
                source, target, kind_str, weight, metadata_str = row
                edge = Edge(
                    source=source,
                    target=target,
                    kind=EdgeKind(kind_str),
                    weight=weight,
                    metadata=json.loads(metadata_str) if metadata_str else {},
                )
                graph.add_edge(edge)

            # Load embeddings
            emb_count = 0
            cur.execute("SELECT node_id, vector FROM embeddings")
            for node_id, blob in cur.fetchall():
                node = graph.get_node(node_id)
                if node and blob:
                    dim = len(blob) // 4  # float32 = 4 bytes each
                    node.embedding = list(struct.unpack(f'{dim}f', blob))
                    emb_count += 1

            logger.info(
                "Loaded graph: %d nodes, %d edges, %d embeddings",
                graph.node_count, graph.edge_count, emb_count,
            )
            return graph
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to load graph: {exc}") from exc

    def save_repo_state(self, repo_name: str, commit_hash: str) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO repo_state (repo_name, commit_hash) VALUES (?, ?)",
                (repo_name, commit_hash),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to save repo state: {exc}") from exc

    def get_repo_state(self, repo_name: str) -> Optional[str]:
        try:
            cur = self._conn.execute(
                "SELECT commit_hash FROM repo_state WHERE repo_name = ?",
                (repo_name,),
            )
            row = cur.fetchone()
            return row[0] if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to get repo state: {exc}") from exc

    def clear(self) -> None:
        """Delete all stored data (nodes, edges, embeddings, repo state)."""
        try:
            self._conn.executescript(
                "DELETE FROM nodes; DELETE FROM edges; "
                "DELETE FROM embeddings; DELETE FROM repo_state;"
            )
            self._conn.commit()
            logger.info("Graph store cleared")
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to clear graph store: {exc}") from exc

    def delete_nodes_by_ids(self, node_ids: list[str]) -> None:
        """Delete specific nodes plus their edges and embeddings."""
        if not node_ids:
            return
        try:
            cur = self._conn.cursor()
            # SQLite has a variable limit, so batch in chunks of 500
            for i in range(0, len(node_ids), 500):
                batch = node_ids[i : i + 500]
                placeholders = ",".join("?" * len(batch))
                cur.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", batch)
                cur.execute(
                    f"DELETE FROM edges WHERE source IN ({placeholders}) OR target IN ({placeholders})",
                    batch + batch,
                )
                cur.execute(f"DELETE FROM embeddings WHERE node_id IN ({placeholders})", batch)
            self._conn.commit()
            logger.info("Deleted %d nodes from graph store", len(node_ids))
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise StorageError(f"Failed to delete nodes: {exc}") from exc

    def get_all_repo_states(self) -> dict[str, str]:
        """Return {repo_name: commit_hash} for every tracked repo."""
        try:
            cur = self._conn.execute("SELECT repo_name, commit_hash FROM repo_state")
            return {row[0]: row[1] for row in cur.fetchall()}
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to get repo states: {exc}") from exc

    def update_retrieval_metadata(self, nodes: list) -> None:
        """Persist retrieval_count, last_retrieved, and co_retrieved_with for the given nodes.

        This is a lightweight partial update — only touches metadata columns,
        not the full node row.
        """
        if not nodes:
            return
        try:
            self._conn.executemany(
                "UPDATE nodes SET retrieval_count = ?, last_retrieved = ?, "
                "co_retrieved_with = ? WHERE id = ?",
                [
                    (n.retrieval_count, n.last_retrieved,
                     json.dumps(n.co_retrieved_with), n.id)
                    for n in nodes
                ],
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Failed to update retrieval metadata: %s", exc)

    def vacuum(self) -> None:
        """Compact the database file to reclaim unused space."""
        try:
            logger.info("Starting database VACUUM...")
            # VACUUM requires its own transaction context usually, but Python's sqlite3
            # handles it fine if we're not currently in an active one.
            self._conn.execute("VACUUM")
            logger.info("Database VACUUM complete.")
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to vacuum database: {exc}") from exc

    def close(self) -> None:
        self._conn.close()
