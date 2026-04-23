"""Feedback domain models — signals and aggregated scores for retrieval learning."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class FeedbackOutcome(str, Enum):
    """Whether the retrieved context was helpful."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class FeedbackSource(str, Enum):
    """How the feedback signal was generated."""

    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


@dataclass
class FeedbackSignal:
    """A single feedback event recorded against a set of retrieved nodes."""

    id: str
    node_ids: list[str]
    outcome: FeedbackOutcome
    source: FeedbackSource
    session_id: Optional[str] = None
    query: Optional[str] = None
    weight: float = 1.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def new(
        node_ids: list[str],
        outcome: FeedbackOutcome | str,
        source: FeedbackSource | str,
        *,
        session_id: Optional[str] = None,
        query: Optional[str] = None,
        weight: float = 1.0,
    ) -> FeedbackSignal:
        return FeedbackSignal(
            id=str(uuid.uuid4()),
            node_ids=node_ids,
            outcome=FeedbackOutcome(outcome),
            source=FeedbackSource(source),
            session_id=session_id,
            query=query,
            weight=weight,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "node_ids": self.node_ids,
            "outcome": self.outcome.value,
            "source": self.source.value,
            "session_id": self.session_id,
            "query": self.query,
            "weight": self.weight,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> FeedbackSignal:
        return cls(
            id=data["id"],
            node_ids=data["node_ids"],
            outcome=FeedbackOutcome(data["outcome"]),
            source=FeedbackSource(data["source"]),
            session_id=data.get("session_id"),
            query=data.get("query"),
            weight=data.get("weight", 1.0),
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


@dataclass
class NodeFeedbackScore:
    """Aggregated feedback score for a single node."""

    node_id: str
    positive_count: int = 0
    negative_count: int = 0
    score: float = 0.5

    @staticmethod
    def compute_score(positive: int, negative: int, smoothing: int = 2) -> float:
        """Laplace-smoothed score: (pos + k) / (pos + neg + 2k).

        Starts at 0.5 (neutral), converges with evidence.
        """
        return (positive + smoothing) / (positive + negative + 2 * smoothing)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "score": round(self.score, 4),
        }
