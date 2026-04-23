"""Tests for SqliteFeedbackStore — CRUD, aggregation, and pair scoring."""

from __future__ import annotations

import pytest
from pathlib import Path

from mimir.domain.feedback import FeedbackSignal, NodeFeedbackScore
from mimir.infra.stores.sqlite_feedback import SqliteFeedbackStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteFeedbackStore:
    return SqliteFeedbackStore(tmp_path / "feedback.db")


class TestRecord:
    def test_record_stores_signal(self, store: SqliteFeedbackStore) -> None:
        signal = FeedbackSignal.new(
            node_ids=["a", "b"], outcome="positive", source="explicit",
        )
        store.record(signal)
        signals = store.list_signals()
        assert len(signals) == 1
        assert signals[0].id == signal.id
        assert signals[0].outcome == "positive"
        assert set(signals[0].node_ids) == {"a", "b"}

    def test_record_updates_node_aggregates(self, store: SqliteFeedbackStore) -> None:
        store.record(FeedbackSignal.new(["n1"], "positive", "explicit"))
        store.record(FeedbackSignal.new(["n1"], "positive", "explicit"))
        store.record(FeedbackSignal.new(["n1"], "negative", "explicit"))

        scores = store.get_node_scores(["n1"])
        assert "n1" in scores
        s = scores["n1"]
        assert s.positive_count == 2
        assert s.negative_count == 1
        # Laplace(2): (2+2)/(2+1+4) = 4/7 ≈ 0.571
        assert abs(s.score - 4 / 7) < 0.01

    def test_node_score_missing_returns_empty(self, store: SqliteFeedbackStore) -> None:
        scores = store.get_node_scores(["nonexistent"])
        assert scores == {}


class TestPairScores:
    def test_pair_aggregation(self, store: SqliteFeedbackStore) -> None:
        store.record(FeedbackSignal.new(["a", "b"], "positive", "explicit"))
        store.record(FeedbackSignal.new(["a", "b"], "positive", "explicit"))
        store.record(FeedbackSignal.new(["a", "b"], "negative", "explicit"))

        pair_scores = store.get_pair_scores("a", ["b"])
        assert "b" in pair_scores
        # Same Laplace as node: (2+2)/(2+1+4) = 4/7
        assert abs(pair_scores["b"] - 4 / 7) < 0.01

    def test_pair_score_missing(self, store: SqliteFeedbackStore) -> None:
        scores = store.get_pair_scores("x", ["y"])
        assert scores == {}

    def test_pair_order_independent(self, store: SqliteFeedbackStore) -> None:
        store.record(FeedbackSignal.new(["b", "a"], "positive", "explicit"))
        # Should be stored as (a, b) regardless of input order
        scores_ab = store.get_pair_scores("a", ["b"])
        scores_ba = store.get_pair_scores("b", ["a"])
        assert scores_ab.get("b") == scores_ba.get("a")


class TestListSignals:
    def test_list_all(self, store: SqliteFeedbackStore) -> None:
        for i in range(5):
            store.record(FeedbackSignal.new([f"n{i}"], "positive", "explicit"))
        assert len(store.list_signals()) == 5

    def test_list_by_session(self, store: SqliteFeedbackStore) -> None:
        store.record(FeedbackSignal.new(["a"], "positive", "explicit", session_id="s1"))
        store.record(FeedbackSignal.new(["b"], "negative", "explicit", session_id="s2"))
        store.record(FeedbackSignal.new(["c"], "positive", "explicit", session_id="s1"))

        s1_signals = store.list_signals(session_id="s1")
        assert len(s1_signals) == 2
        assert all(s.session_id == "s1" for s in s1_signals)

    def test_list_limit(self, store: SqliteFeedbackStore) -> None:
        for i in range(10):
            store.record(FeedbackSignal.new([f"n{i}"], "positive", "explicit"))
        assert len(store.list_signals(limit=3)) == 3


class TestClear:
    def test_clear_removes_all(self, store: SqliteFeedbackStore) -> None:
        store.record(FeedbackSignal.new(["a", "b"], "positive", "explicit"))
        store.clear()
        assert store.list_signals() == []
        assert store.get_node_scores(["a"]) == {}
        assert store.get_pair_scores("a", ["b"]) == {}


class TestLaplaceSmoothing:
    def test_neutral_start(self) -> None:
        assert NodeFeedbackScore.compute_score(0, 0, smoothing=2) == 0.5

    def test_all_positive(self) -> None:
        score = NodeFeedbackScore.compute_score(10, 0, smoothing=2)
        # (10+2)/(10+0+4) = 12/14 ≈ 0.857
        assert abs(score - 12 / 14) < 0.001

    def test_all_negative(self) -> None:
        score = NodeFeedbackScore.compute_score(0, 10, smoothing=2)
        # (0+2)/(0+10+4) = 2/14 ≈ 0.143
        assert abs(score - 2 / 14) < 0.001

    def test_custom_smoothing(self) -> None:
        score = NodeFeedbackScore.compute_score(5, 5, smoothing=1)
        # (5+1)/(5+5+2) = 6/12 = 0.5
        assert score == 0.5
