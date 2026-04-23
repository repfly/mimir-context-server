"""FeedbackStore port — interface for persisting retrieval feedback."""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from mimir.domain.feedback import FeedbackSignal, NodeFeedbackScore


@runtime_checkable
class FeedbackStore(Protocol):
    """Interface for durable feedback storage.

    Implementation: ``SqliteFeedbackStore``.
    """

    def record(self, signal: FeedbackSignal) -> None:
        """Persist a feedback signal and update aggregated scores."""
        ...

    def get_node_scores(self, node_ids: list[str]) -> dict[str, NodeFeedbackScore]:
        """Return aggregated feedback scores for the given node IDs."""
        ...

    def get_pair_scores(self, node_id: str, candidate_ids: list[str]) -> dict[str, float]:
        """Return co-success affinity scores between *node_id* and each candidate."""
        ...

    def list_signals(
        self,
        session_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[FeedbackSignal]:
        """Return recent feedback signals, optionally filtered by session."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...
