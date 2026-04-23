"""SQLite-backed feedback persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from itertools import combinations
from pathlib import Path
from typing import Optional

from mimir.domain.errors import StorageError
from mimir.domain.feedback import FeedbackOutcome, FeedbackSignal, NodeFeedbackScore

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_signals (
    id         TEXT PRIMARY KEY,
    session_id TEXT,
    query      TEXT,
    node_ids   TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    source     TEXT NOT NULL,
    weight     REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS node_feedback_agg (
    node_id        TEXT PRIMARY KEY,
    positive_count INTEGER DEFAULT 0,
    negative_count INTEGER DEFAULT 0,
    score          REAL DEFAULT 0.5,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS pair_feedback_agg (
    node_a      TEXT NOT NULL,
    node_b      TEXT NOT NULL,
    co_positive INTEGER DEFAULT 0,
    co_negative INTEGER DEFAULT 0,
    score       REAL DEFAULT 0.5,
    PRIMARY KEY (node_a, node_b)
);

CREATE INDEX IF NOT EXISTS idx_feedback_session ON feedback_signals(session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback_signals(created_at);
CREATE INDEX IF NOT EXISTS idx_pair_node_a ON pair_feedback_agg(node_a);
"""

#: Signals older than this many days are purged on startup.
_EXPIRY_DAYS = 90

#: Laplace smoothing parameter for score computation.
_DEFAULT_SMOOTHING = 2


class SqliteFeedbackStore:
    """SQLite persistence for retrieval feedback signals and aggregated scores."""

    def __init__(self, db_path: Path, *, smoothing: int = _DEFAULT_SMOOTHING) -> None:
        self._db_path = db_path
        self._smoothing = smoothing
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._purge_expired()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to init feedback store: {exc}") from exc
        logger.info("Feedback store initialised at %s", db_path)

    def _purge_expired(self) -> None:
        try:
            cur = self._conn.execute(
                "DELETE FROM feedback_signals WHERE created_at < datetime('now', ?)",
                (f"-{_EXPIRY_DAYS} days",),
            )
            if cur.rowcount > 0:
                self._conn.commit()
                logger.info(
                    "Purged %d expired feedback signals (older than %d days)",
                    cur.rowcount,
                    _EXPIRY_DAYS,
                )
        except sqlite3.Error:
            pass

    def record(self, signal: FeedbackSignal) -> None:
        """Persist a feedback signal and incrementally update aggregated scores."""
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO feedback_signals "
                "(id, session_id, query, node_ids, outcome, source, weight, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    signal.id,
                    signal.session_id,
                    signal.query,
                    json.dumps(signal.node_ids),
                    signal.outcome,
                    signal.source,
                    signal.weight,
                    signal.timestamp.isoformat(),
                ),
            )
            self._update_node_aggregates(signal)
            self._update_pair_aggregates(signal)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to record feedback: {exc}") from exc

    def _update_node_aggregates(self, signal: FeedbackSignal) -> None:
        is_positive = signal.outcome is FeedbackOutcome.POSITIVE
        for node_id in signal.node_ids:
            self._conn.execute(
                "INSERT INTO node_feedback_agg (node_id, positive_count, negative_count, score, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "positive_count = positive_count + ?, "
                "negative_count = negative_count + ?, "
                "updated_at = datetime('now')",
                (
                    node_id,
                    1 if is_positive else 0,
                    0 if is_positive else 1,
                    NodeFeedbackScore.compute_score(
                        1 if is_positive else 0,
                        0 if is_positive else 1,
                        self._smoothing,
                    ),
                    1 if is_positive else 0,
                    0 if is_positive else 1,
                ),
            )
            # Recompute score from actual counts
            cur = self._conn.execute(
                "SELECT positive_count, negative_count FROM node_feedback_agg WHERE node_id = ?",
                (node_id,),
            )
            row = cur.fetchone()
            if row:
                score = NodeFeedbackScore.compute_score(row[0], row[1], self._smoothing)
                self._conn.execute(
                    "UPDATE node_feedback_agg SET score = ? WHERE node_id = ?",
                    (score, node_id),
                )

    def _update_pair_aggregates(self, signal: FeedbackSignal) -> None:
        if len(signal.node_ids) < 2:
            return
        is_positive = signal.outcome is FeedbackOutcome.POSITIVE
        for a, b in combinations(sorted(signal.node_ids), 2):
            self._conn.execute(
                "INSERT INTO pair_feedback_agg (node_a, node_b, co_positive, co_negative, score) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(node_a, node_b) DO UPDATE SET "
                "co_positive = co_positive + ?, "
                "co_negative = co_negative + ?",
                (
                    a, b,
                    1 if is_positive else 0,
                    0 if is_positive else 1,
                    NodeFeedbackScore.compute_score(
                        1 if is_positive else 0,
                        0 if is_positive else 1,
                        self._smoothing,
                    ),
                    1 if is_positive else 0,
                    0 if is_positive else 1,
                ),
            )
            # Recompute pair score
            cur = self._conn.execute(
                "SELECT co_positive, co_negative FROM pair_feedback_agg "
                "WHERE node_a = ? AND node_b = ?",
                (a, b),
            )
            row = cur.fetchone()
            if row:
                score = NodeFeedbackScore.compute_score(row[0], row[1], self._smoothing)
                self._conn.execute(
                    "UPDATE pair_feedback_agg SET score = ? WHERE node_a = ? AND node_b = ?",
                    (score, a, b),
                )

    def get_node_scores(self, node_ids: list[str]) -> dict[str, NodeFeedbackScore]:
        if not node_ids:
            return {}
        placeholders = ",".join("?" for _ in node_ids)
        try:
            cur = self._conn.execute(
                f"SELECT node_id, positive_count, negative_count, score "
                f"FROM node_feedback_agg WHERE node_id IN ({placeholders})",
                node_ids,
            )
            return {
                row[0]: NodeFeedbackScore(
                    node_id=row[0],
                    positive_count=row[1],
                    negative_count=row[2],
                    score=row[3],
                )
                for row in cur.fetchall()
            }
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to get node scores: {exc}") from exc

    def get_pair_scores(self, node_id: str, candidate_ids: list[str]) -> dict[str, float]:
        if not candidate_ids:
            return {}
        results: dict[str, float] = {}
        try:
            for cid in candidate_ids:
                a, b = sorted([node_id, cid])
                cur = self._conn.execute(
                    "SELECT score FROM pair_feedback_agg WHERE node_a = ? AND node_b = ?",
                    (a, b),
                )
                row = cur.fetchone()
                if row:
                    results[cid] = row[0]
            return results
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to get pair scores: {exc}") from exc

    def list_signals(
        self,
        session_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[FeedbackSignal]:
        try:
            if session_id:
                cur = self._conn.execute(
                    "SELECT id, session_id, query, node_ids, outcome, source, weight, created_at "
                    "FROM feedback_signals WHERE session_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT id, session_id, query, node_ids, outcome, source, weight, created_at "
                    "FROM feedback_signals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [
                FeedbackSignal.from_dict({
                    "id": row[0],
                    "session_id": row[1],
                    "query": row[2],
                    "node_ids": json.loads(row[3]),
                    "outcome": row[4],
                    "source": row[5],
                    "weight": row[6],
                    "timestamp": row[7],
                })
                for row in cur.fetchall()
            ]
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to list signals: {exc}") from exc

    def clear(self) -> None:
        """Delete all feedback data."""
        try:
            self._conn.execute("DELETE FROM feedback_signals")
            self._conn.execute("DELETE FROM node_feedback_agg")
            self._conn.execute("DELETE FROM pair_feedback_agg")
            self._conn.commit()
            logger.info("Feedback store cleared")
        except sqlite3.Error as exc:
            raise StorageError(f"Failed to clear feedback store: {exc}") from exc

    def close(self) -> None:
        self._conn.close()
