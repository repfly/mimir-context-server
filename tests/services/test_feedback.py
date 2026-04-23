"""Tests for FeedbackService — explicit recording, implicit heuristics, weight retrieval."""

from __future__ import annotations

import pytest
from pathlib import Path
from datetime import datetime, timezone

from mimir.domain.config import MimirConfig, RepoConfig, FeedbackConfig
from mimir.domain.feedback import FeedbackOutcome, FeedbackSignal, FeedbackSource
from mimir.domain.session import Session, QueryRecord
from mimir.infra.stores.sqlite_feedback import SqliteFeedbackStore
from mimir.services.feedback import FeedbackService


def _make_config(tmp_path: Path, **feedback_kw) -> MimirConfig:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    return MimirConfig(
        repos=[RepoConfig(name="test", path=repo_dir)],
        data_dir=tmp_path / ".mimir",
        feedback=FeedbackConfig(**feedback_kw),
    )


@pytest.fixture
def feedback_env(tmp_path: Path):
    config = _make_config(tmp_path)
    store = SqliteFeedbackStore(tmp_path / "feedback.db")
    service = FeedbackService(config=config, feedback_store=store)
    return service, store


class TestExplicitFeedback:
    def test_record_positive(self, feedback_env) -> None:
        service, store = feedback_env
        signal = service.record_explicit(["n1", "n2"], "positive", session_id="s1")
        assert signal.outcome is FeedbackOutcome.POSITIVE
        assert signal.source is FeedbackSource.EXPLICIT
        assert store.list_signals() != []

    def test_record_negative(self, feedback_env) -> None:
        service, store = feedback_env
        signal = service.record_explicit(["n1"], "negative")
        assert signal.outcome is FeedbackOutcome.NEGATIVE

    def test_disabled_skips_store(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, enabled=False)
        store = SqliteFeedbackStore(tmp_path / "feedback.db")
        service = FeedbackService(config=config, feedback_store=store)
        service.record_explicit(["n1"], "positive")
        assert store.list_signals() == []


class TestImplicitFeedback:
    @staticmethod
    def _make_session_with_queries(
        embeddings: list[list[float]],
        node_ids: list[list[str]],
    ) -> Session:
        session = Session(session_id="test-session")
        for i, (emb, nids) in enumerate(zip(embeddings, node_ids)):
            session.record_query(
                query=f"query {i}",
                retrieved_ids=nids,
                relevance_scores={nid: 1.0 for nid in nids},
                query_embedding=emb,
            )
        return session

    def test_similar_queries_negative(self, feedback_env) -> None:
        """Follow-up with high similarity → negative signal."""
        service, store = feedback_env
        # Two nearly identical embeddings (cosine > 0.7)
        emb1 = [1.0, 0.0, 0.0]
        emb2 = [0.99, 0.1, 0.0]
        session = self._make_session_with_queries(
            [emb1, emb2], [["n1", "n2"], ["n3"]],
        )
        signal = service.record_implicit(session)
        assert signal is not None
        assert signal.outcome is FeedbackOutcome.NEGATIVE
        assert set(signal.node_ids) == {"n1", "n2"}

    def test_different_queries_positive(self, feedback_env) -> None:
        """Topic shift → positive signal."""
        service, store = feedback_env
        emb1 = [1.0, 0.0, 0.0]
        emb2 = [0.0, 1.0, 0.0]  # orthogonal → cosine ≈ 0
        session = self._make_session_with_queries(
            [emb1, emb2], [["n1"], ["n2"]],
        )
        signal = service.record_implicit(session)
        assert signal is not None
        assert signal.outcome is FeedbackOutcome.POSITIVE
        assert signal.node_ids == ["n1"]

    def test_medium_similarity_no_signal(self, feedback_env) -> None:
        """Mid-range similarity → no signal emitted."""
        service, store = feedback_env
        emb1 = [1.0, 0.0, 0.0]
        emb2 = [0.5, 0.866, 0.0]  # cosine ≈ 0.5
        session = self._make_session_with_queries(
            [emb1, emb2], [["n1"], ["n2"]],
        )
        signal = service.record_implicit(session)
        assert signal is None

    def test_single_query_no_signal(self, feedback_env) -> None:
        """Only one query → not enough history."""
        service, _ = feedback_env
        session = self._make_session_with_queries(
            [[1.0, 0.0]], [["n1"]],
        )
        signal = service.record_implicit(session)
        assert signal is None

    def test_disabled_implicit(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, implicit_signals=False)
        store = SqliteFeedbackStore(tmp_path / "feedback.db")
        service = FeedbackService(config=config, feedback_store=store)
        session = self._make_session_with_queries(
            [[1.0, 0.0], [1.0, 0.0]], [["n1"], ["n2"]],
        )
        assert service.record_implicit(session) is None

    def test_no_embeddings_no_signal(self, feedback_env) -> None:
        """Queries without embeddings → can't compute similarity."""
        service, _ = feedback_env
        session = Session(session_id="s")
        session.record_query("q1", ["n1"], {"n1": 1.0})
        session.record_query("q2", ["n2"], {"n2": 1.0})
        assert service.record_implicit(session) is None


class TestWeightRetrieval:
    def test_get_weight_with_data(self, feedback_env) -> None:
        service, store = feedback_env
        service.record_explicit(["n1"], "positive")
        service.record_explicit(["n1"], "positive")
        w = service.get_feedback_weight("n1")
        assert w > 0.5

    def test_get_weight_no_data(self, feedback_env) -> None:
        service, _ = feedback_env
        assert service.get_feedback_weight("unknown") == 0.5

    def test_get_weights_batch(self, feedback_env) -> None:
        service, _ = feedback_env
        service.record_explicit(["n1"], "positive")
        service.record_explicit(["n2"], "negative")
        weights = service.get_feedback_weights(["n1", "n2", "n3"])
        assert weights["n1"] > 0.5
        assert weights["n2"] < 0.5
        assert weights["n3"] == 0.5

    def test_disabled_returns_neutral(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, enabled=False)
        store = SqliteFeedbackStore(tmp_path / "feedback.db")
        service = FeedbackService(config=config, feedback_store=store)
        assert service.get_feedback_weight("n1") == 0.5
        assert service.get_feedback_weights(["n1", "n2"]) == {"n1": 0.5, "n2": 0.5}
