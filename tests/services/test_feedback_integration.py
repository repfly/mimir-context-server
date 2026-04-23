"""Integration test: feedback signals flow through to temporal reranking weights."""

from __future__ import annotations

import pytest
from pathlib import Path

from mimir.domain.config import MimirConfig, RepoConfig, FeedbackConfig, TemporalConfig
from mimir.domain.models import Node, NodeKind
from mimir.domain.subgraph import SubGraph
from mimir.infra.stores.sqlite_feedback import SqliteFeedbackStore
from mimir.services.feedback import FeedbackService
from mimir.services.temporal import TemporalService


def _make_config(tmp_path: Path) -> MimirConfig:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    return MimirConfig(
        repos=[RepoConfig(name="test", path=repo_dir)],
        data_dir=tmp_path / ".mimir",
        feedback=FeedbackConfig(enabled=True),
        temporal=TemporalConfig(co_retrieval_enabled=False),
    )


def _make_node(node_id: str) -> Node:
    return Node(id=node_id, repo="test", kind=NodeKind.FUNCTION, name=node_id)


@pytest.fixture
def env(tmp_path: Path):
    config = _make_config(tmp_path)
    store = SqliteFeedbackStore(tmp_path / "feedback.db")
    feedback = FeedbackService(config=config, feedback_store=store)
    temporal = TemporalService(config=config)
    temporal.set_feedback_service(feedback)
    return feedback, temporal


class TestFeedbackTemporalIntegration:
    def test_positive_feedback_boosts_score(self, env) -> None:
        feedback, temporal = env
        # Record positive feedback for node_a
        for _ in range(5):
            feedback.record_explicit(["node_a"], "positive")

        subgraph = SubGraph()
        node_a = _make_node("node_a")
        node_b = _make_node("node_b")
        subgraph.add_node(node_a, score=0.5)
        subgraph.add_node(node_b, score=0.5)

        temporal.apply_temporal_weights(subgraph)

        # node_a should score higher than node_b (which has no feedback)
        assert subgraph.scores["node_a"] > subgraph.scores["node_b"]

    def test_negative_feedback_lowers_score(self, env) -> None:
        feedback, temporal = env
        for _ in range(5):
            feedback.record_explicit(["node_a"], "negative")

        subgraph = SubGraph()
        node_a = _make_node("node_a")
        node_b = _make_node("node_b")
        subgraph.add_node(node_a, score=0.5)
        subgraph.add_node(node_b, score=0.5)

        temporal.apply_temporal_weights(subgraph)

        assert subgraph.scores["node_a"] < subgraph.scores["node_b"]

    def test_no_feedback_is_neutral(self, env) -> None:
        _, temporal = env
        subgraph = SubGraph()
        node_a = _make_node("node_a")
        node_b = _make_node("node_b")
        subgraph.add_node(node_a, score=0.5)
        subgraph.add_node(node_b, score=0.5)

        temporal.apply_temporal_weights(subgraph)

        # Without any feedback data, both should be equal
        assert abs(subgraph.scores["node_a"] - subgraph.scores["node_b"]) < 0.001

    def test_backward_compat_without_feedback_service(self, tmp_path: Path) -> None:
        """Temporal weights work correctly without feedback service."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config = MimirConfig(
            repos=[RepoConfig(name="test", path=repo_dir)],
            data_dir=tmp_path / ".mimir",
            temporal=TemporalConfig(co_retrieval_enabled=False),
        )
        temporal = TemporalService(config=config)
        # Deliberately NOT setting feedback service

        subgraph = SubGraph()
        subgraph.add_node(_make_node("node_a"), score=0.8)
        subgraph.add_node(_make_node("node_b"), score=0.3)

        temporal.apply_temporal_weights(subgraph)

        # Should use the 4-term formula, higher initial score → higher final
        assert subgraph.scores["node_a"] > subgraph.scores["node_b"]
