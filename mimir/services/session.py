"""Session service — conversation-aware retrieval, dedup, topic tracking."""

from __future__ import annotations

import logging
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

    def session_dedup(self, subgraph: SubGraph, session: Session) -> None:
        """Remove or summarize nodes already in the LLM's context.

        Rules:
        - Turn -1 (last turn): skip entirely
        - Turn -2 to -3: keep summary only, remove code
        - Turn -5+: re-include fully (LLM has forgotten)
        - Modified since last provided: always re-include
        """
        current_turn = session.current_turn

        for node_id in list(subgraph.node_ids):
            entry = session.context_window.get(node_id)
            if entry is None:
                continue

            turns_ago = current_turn - entry.turn_number
            node = subgraph.nodes.get(node_id)
            if node is None:
                continue

            if turns_ago <= 1:
                # Just provided — skip
                subgraph.remove_node(node_id)
                subgraph.add_note(
                    f"{node.name} already in context (turn {entry.turn_number})"
                )
            elif turns_ago <= 3:
                # Recent — summary only
                node.raw_code = None
            # else: re-include fully

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
