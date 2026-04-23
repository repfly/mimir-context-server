"""Feedback service — explicit and implicit retrieval feedback loop.

Records feedback signals against retrieved node sets and exposes
aggregated scores that feed into temporal reranking.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from mimir.domain.config import MimirConfig
from mimir.domain.feedback import (
    FeedbackOutcome,
    FeedbackSignal,
    FeedbackSource,
    NodeFeedbackScore,
)
from mimir.domain.session import Session
from mimir.ports.feedback_store import FeedbackStore

logger = logging.getLogger(__name__)


class FeedbackService:
    """Manages the retrieval feedback loop."""

    def __init__(self, config: MimirConfig, feedback_store: FeedbackStore) -> None:
        self._config = config
        self._store = feedback_store
        self._enabled = config.feedback.enabled
        self._implicit_enabled = config.feedback.implicit_signals
        self._implicit_pos_weight = config.feedback.implicit_positive_weight
        self._implicit_neg_weight = config.feedback.implicit_negative_weight

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record_explicit(
        self,
        node_ids: list[str],
        outcome: FeedbackOutcome | str,
        *,
        session_id: Optional[str] = None,
        query: Optional[str] = None,
    ) -> FeedbackSignal:
        """Record explicit feedback from an agent or user."""
        signal = FeedbackSignal.new(
            node_ids=node_ids, outcome=outcome, source=FeedbackSource.EXPLICIT,
            session_id=session_id, query=query, weight=1.0,
        )
        if not self._enabled:
            logger.debug("Feedback disabled, skipping explicit signal")
            return signal
        self._store.record(signal)
        logger.info(
            "Recorded explicit %s feedback for %d nodes (session=%s)",
            outcome, len(node_ids), session_id,
        )
        return signal

    def record_implicit(self, session: Session) -> Optional[FeedbackSignal]:
        """Analyze session history and emit implicit feedback.

        Heuristics:
        - Consecutive queries with high topic similarity (cosine > 0.7):
          prior retrieval was insufficient -> negative signal.
        - Topic shift (cosine < 0.3): prior retrieval resolved the
          question -> positive signal.
        """
        if not self._enabled or not self._implicit_enabled:
            return None
        history = session.query_history
        if len(history) < 2:
            return None
        prev, curr = history[-2], history[-1]
        if not prev.retrieved_node_ids:
            return None
        similarity = self._query_similarity(prev, curr)
        if similarity is None:
            return None

        if similarity > 0.7:
            signal = FeedbackSignal.new(
                node_ids=prev.retrieved_node_ids,
                outcome=FeedbackOutcome.NEGATIVE,
                source=FeedbackSource.IMPLICIT,
                session_id=session.session_id,
                query=prev.query, weight=self._implicit_neg_weight,
            )
            self._store.record(signal)
            logger.debug("Implicit negative: follow-up (sim=%.2f)", similarity)
            return signal

        if similarity < 0.3:
            signal = FeedbackSignal.new(
                node_ids=prev.retrieved_node_ids,
                outcome=FeedbackOutcome.POSITIVE,
                source=FeedbackSource.IMPLICIT,
                session_id=session.session_id,
                query=prev.query, weight=self._implicit_pos_weight,
            )
            self._store.record(signal)
            logger.debug("Implicit positive: topic shift (sim=%.2f)", similarity)
            return signal

        return None

    @staticmethod
    def _query_similarity(prev, curr) -> Optional[float]:
        """Cosine similarity between two query records' embeddings."""
        a, b = prev.query_embedding, curr.query_embedding
        if a is None or b is None:
            return None
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def get_feedback_weight(self, node_id: str) -> float:
        """Return the feedback score for a single node (0.0-1.0, neutral=0.5)."""
        if not self._enabled:
            return 0.5
        scores = self._store.get_node_scores([node_id])
        return scores[node_id].score if node_id in scores else 0.5

    def get_feedback_weights(self, node_ids: list[str]) -> dict[str, float]:
        """Return feedback scores for multiple nodes. Missing nodes get 0.5."""
        if not self._enabled:
            return {nid: 0.5 for nid in node_ids}
        scores = self._store.get_node_scores(node_ids)
        return {nid: scores[nid].score if nid in scores else 0.5 for nid in node_ids}

    def get_node_score(self, node_id: str) -> Optional[NodeFeedbackScore]:
        """Return the full feedback score object for a node, or None."""
        if not self._enabled:
            return None
        scores = self._store.get_node_scores([node_id])
        return scores.get(node_id)
