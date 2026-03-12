"""Session service — conversation-aware retrieval, dedup, topic tracking."""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from mimir.domain.config import MimirConfig
from mimir.domain.models import Node
from mimir.domain.session import Session
from mimir.domain.subgraph import SubGraph
from mimir.ports.session_store import SessionStore

logger = logging.getLogger(__name__)


class SessionService:
    """Manages conversation state for multi-turn retrieval."""

    def __init__(
        self,
        config: MimirConfig,
        session_store: SessionStore,
    ) -> None:
        self._config = config
        self._store = session_store
        self._decay_turns = config.session.context_decay_turns
        self._topic_alpha = config.session.topic_tracking_alpha

    def get_or_create(self, session_id: Optional[str] = None) -> Session:
        """Load an existing session or create a new one."""
        if session_id:
            session = self._store.load(session_id)
            if session:
                return session

        new_id = session_id or str(uuid.uuid4())
        session = Session(session_id=new_id)
        self._store.save(session)
        logger.info("Created new session: %s", new_id)
        return session

    def session_dedup(
        self,
        subgraph: SubGraph,
        session: Session,
        query_embedding: Optional[list[float]] = None,
    ) -> None:
        """Remove or summarize nodes using continuous exponential decay.

        Uses ``context_decay_turns`` as the half-life for an exponential
        decay model.  Nodes whose topic is still relevant (measured by
        cosine similarity between current query and the query that added
        them) decay slower — the LLM is more likely to remember them.

        Decay weight thresholds:
        - > 0.8  → skip (LLM still remembers)
        - 0.3–0.8 → summary only (fading from memory)
        - < 0.3  → re-include fully (forgotten)
        """
        current_turn = session.current_turn
        half_life = max(self._decay_turns, 1)
        decay_lambda = math.log(2) / half_life

        for node_id in list(subgraph.node_ids):
            entry = session.context_window.get(node_id)
            if entry is None:
                continue

            turns_ago = current_turn - entry.turn_number
            node = subgraph.nodes.get(node_id)
            if node is None:
                continue

            # Base decay weight: 1.0 at turn 0, 0.5 at half_life turns ago
            decay_weight = math.exp(-decay_lambda * turns_ago)

            # Topic similarity bonus: if current query is close to the query
            # that originally added this node, the LLM is more likely to still
            # remember it — boost the decay weight.
            if query_embedding and entry.query_embedding_at_addition:
                topic_sim = self._cosine_similarity(
                    query_embedding, entry.query_embedding_at_addition
                )
                # Boost: up to +0.2 for highly similar topics
                decay_weight = min(1.0, decay_weight + 0.2 * max(0.0, topic_sim))

            if decay_weight > 0.8:
                # LLM still remembers — skip
                subgraph.remove_node(node_id)
                subgraph.add_note(
                    f"{node.name} already in context (turn {entry.turn_number})"
                )
            elif decay_weight > 0.3:
                # Fading — summary only
                node.raw_code = None
            # else: re-include fully (forgotten)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def update_topic(self, session: Session, query_embedding: list[float]) -> None:
        """Update session topic vector using exponential moving average."""
        if session.session_topic_embedding is None:
            session.session_topic_embedding = query_embedding
        else:
            alpha = self._topic_alpha
            session.session_topic_embedding = [
                alpha * q + (1 - alpha) * s
                for q, s in zip(query_embedding, session.session_topic_embedding)
            ]

    def record_retrieval(
        self,
        session: Session,
        query: str,
        nodes: list[Node],
        scores: dict[str, float],
        query_embedding: Optional[list[float]] = None,
    ) -> None:
        """Record a query and its results in the session."""
        session.record_query(
            query=query,
            retrieved_ids=[n.id for n in nodes],
            relevance_scores=scores,
            query_embedding=query_embedding,
        )
        if query_embedding:
            self.update_topic(session, query_embedding)
        self._store.save(session)

    def get_session_state(self, session_id: str) -> Optional[dict]:
        """Return session state summary for display."""
        session = self._store.load(session_id)
        if not session:
            return None
        return {
            "session_id": session.session_id,
            "started_at": session.started_at.isoformat(),
            "current_turn": session.current_turn,
            "context_window_size": len(session.context_window),
            "query_count": len(session.query_history),
            "recent_queries": [
                q.query for q in session.query_history[-5:]
            ],
        }

    def list_sessions(self) -> list[str]:
        return self._store.list_sessions()
