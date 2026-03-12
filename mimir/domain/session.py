"""Session domain models — conversation state for multi-turn retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ContextEntry:
    """Record of a node that was sent to the LLM."""

    node_id: str
    added_at: datetime
    turn_number: int
    relevance_at_addition: float
    query_embedding_at_addition: Optional[list[float]] = None

    def to_dict(self) -> dict:
        d = {
            "node_id": self.node_id,
            "added_at": self.added_at.isoformat(),
            "turn_number": self.turn_number,
            "relevance_at_addition": self.relevance_at_addition,
        }
        if self.query_embedding_at_addition is not None:
            d["query_embedding_at_addition"] = self.query_embedding_at_addition
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ContextEntry:
        return cls(
            node_id=data["node_id"],
            added_at=datetime.fromisoformat(data["added_at"]),
            turn_number=data["turn_number"],
            relevance_at_addition=data["relevance_at_addition"],
            query_embedding_at_addition=data.get("query_embedding_at_addition"),
        )


@dataclass
class QueryRecord:
    """Record of a single query within a session."""

    query: str
    turn_number: int
    retrieved_node_ids: list[str]
    timestamp: datetime
    query_embedding: Optional[list[float]] = None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "turn_number": self.turn_number,
            "retrieved_node_ids": self.retrieved_node_ids,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> QueryRecord:
        return cls(
            query=data["query"],
            turn_number=data["turn_number"],
            retrieved_node_ids=data["retrieved_node_ids"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


@dataclass
class Session:
    """Tracks what the LLM currently knows for session-aware retrieval."""

    session_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # What the LLM currently has in context
    context_window: dict[str, ContextEntry] = field(default_factory=dict)

    # History of queries in this session
    query_history: list[QueryRecord] = field(default_factory=list)

    # Running average of query embeddings — represents the session topic
    session_topic_embedding: Optional[list[float]] = None

    @property
    def current_turn(self) -> int:
        if not self.query_history:
            return 0
        return self.query_history[-1].turn_number

    def record_query(
        self,
        query: str,
        retrieved_ids: list[str],
        relevance_scores: dict[str, float],
        *,
        query_embedding: Optional[list[float]] = None,
    ) -> None:
        """Record a query and update the context window."""
        turn = self.current_turn + 1
        now = datetime.now(timezone.utc)

        self.query_history.append(QueryRecord(
            query=query,
            turn_number=turn,
            retrieved_node_ids=retrieved_ids,
            timestamp=now,
            query_embedding=query_embedding,
        ))

        for node_id in retrieved_ids:
            self.context_window[node_id] = ContextEntry(
                node_id=node_id,
                added_at=now,
                turn_number=turn,
                relevance_at_addition=relevance_scores.get(node_id, 0.0),
                query_embedding_at_addition=query_embedding,
            )

    def prune(self, max_context_entries: int = 500, max_query_history: int = 100) -> None:
        """Prune old context entries and query history to bound session size.

        Keeps the most recent entries by turn number.
        """
        if len(self.context_window) > max_context_entries:
            sorted_entries = sorted(
                self.context_window.items(),
                key=lambda kv: kv[1].turn_number,
                reverse=True,
            )
            self.context_window = dict(sorted_entries[:max_context_entries])

        if len(self.query_history) > max_query_history:
            self.query_history = self.query_history[-max_query_history:]

    def to_dict(self) -> dict:
        d = {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "context_window": {
                k: v.to_dict() for k, v in self.context_window.items()
            },
            "query_history": [q.to_dict() for q in self.query_history],
        }
        if self.session_topic_embedding is not None:
            d["session_topic_embedding"] = self.session_topic_embedding
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        session = cls(
            session_id=data["session_id"],
            started_at=datetime.fromisoformat(data["started_at"]),
        )
        session.context_window = {
            k: ContextEntry.from_dict(v)
            for k, v in data.get("context_window", {}).items()
        }
        session.query_history = [
            QueryRecord.from_dict(q)
            for q in data.get("query_history", [])
        ]
        session.session_topic_embedding = data.get("session_topic_embedding")
        return session
