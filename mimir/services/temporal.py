"""Temporal service — recency weighting, hotspot detection, co-retrieval learning."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from mimir.domain.config import MimirConfig
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node
from mimir.domain.subgraph import SubGraph

if TYPE_CHECKING:
    from mimir.services.quality import QualityService

logger = logging.getLogger(__name__)


class TemporalService:
    """Manages temporal signals for context assembly."""

    def __init__(self, config: MimirConfig) -> None:
        self._lambda = config.temporal.recency_lambda
        self._change_window = config.temporal.change_window_commits
        self._co_retrieval_enabled = config.temporal.co_retrieval_enabled
        self._quality_service: Optional[QualityService] = None

    def set_quality_service(self, quality_service: QualityService) -> None:
        """Inject the quality service for connectivity-aware reranking."""
        self._quality_service = quality_service

    def temporal_weight(self, node: Node, now: Optional[datetime] = None) -> float:
        """Exponential decay based on last modification time.

        half-life ≈ ln(2) / lambda ≈ 35 days for lambda=0.02
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if node.last_modified is None:
            return 0.5  # unknown → neutral

        try:
            modified = datetime.fromisoformat(node.last_modified)
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            days = max(0, (now - modified).days)
            return math.exp(-self._lambda * days)
        except (ValueError, TypeError):
            return 0.5

    def apply_temporal_weights(
        self, subgraph: SubGraph, graph: Optional[CodeGraph] = None,
    ) -> None:
        """Rerank nodes in a subgraph using temporal and quality signals.

        When a quality service is available and a graph is provided:
            final = 0.45*retrieval + 0.18*recency + 0.12*change_freq
                  + 0.12*co_retrieval + 0.13*quality

        Without quality:
            final = 0.50*retrieval + 0.20*recency + 0.15*change_freq
                  + 0.15*co_retrieval
        """
        now = datetime.now(timezone.utc)
        use_quality = self._quality_service is not None and graph is not None

        for node_id, node in subgraph.nodes.items():
            retrieval_score = subgraph.scores.get(node_id, 0.5)
            recency = self.temporal_weight(node, now)
            change_freq = self._change_frequency_weight(node)
            co_ret = self._co_retrieval_weight(node, subgraph) if self._co_retrieval_enabled else 0.0

            if use_quality:
                quality = self._quality_service.compute_quality_score(node, graph)
                final = (
                    0.45 * retrieval_score
                    + 0.18 * recency
                    + 0.12 * change_freq
                    + 0.12 * co_ret
                    + 0.13 * quality
                )
            else:
                final = (
                    0.5 * retrieval_score
                    + 0.2 * recency
                    + 0.15 * change_freq
                    + 0.15 * co_ret
                )
            subgraph.scores[node_id] = final

    @staticmethod
    def _change_frequency_weight(node: Node) -> float:
        """Normalize modification count to [0, 1]."""
        if node.modification_count <= 0:
            return 0.0
        # sigmoid-like normalization: 10 changes → ~0.5, 50 → ~0.9
        return 1.0 - 1.0 / (1.0 + node.modification_count / 10.0)

    @staticmethod
    def _co_retrieval_weight(node: Node, subgraph: SubGraph) -> float:
        """How often this node has been co-retrieved with other subgraph nodes."""
        if not node.co_retrieved_with:
            return 0.0
        total = sum(
            count for other_id, count in node.co_retrieved_with.items()
            if other_id in subgraph.node_ids
        )
        # Normalize: 10 co-retrievals → ~0.5
        return 1.0 - 1.0 / (1.0 + total / 10.0)

    def update_co_retrieval(self, retrieved_nodes: list[Node]) -> None:
        """Update co-occurrence counts after a retrieval."""
        if not self._co_retrieval_enabled:
            return
        ids = [n.id for n in retrieved_nodes]
        for i, node in enumerate(retrieved_nodes):
            for j, other_id in enumerate(ids):
                if i != j:
                    node.co_retrieved_with[other_id] = (
                        node.co_retrieved_with.get(other_id, 0) + 1
                    )

    def get_hotspots(
        self,
        graph: CodeGraph,
        top_n: int = 20,
    ) -> list[tuple[Node, float]]:
        """Return the most frequently changed code (active development areas)."""
        scored: list[tuple[Node, float]] = []
        for node in graph.symbol_nodes():
            if node.modification_count > 0:
                recency = self.temporal_weight(node)
                combined = 0.6 * self._change_frequency_weight(node) + 0.4 * recency
                scored.append((node, combined))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]
