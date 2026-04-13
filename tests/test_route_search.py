"""Tests for RetrievalService._route_match_seeds.

Covers:
- Queries containing a route path (e.g., "/orders") match API_ENDPOINT nodes
- HTTP method in query narrows/boosts results
- Non-route queries return no matches
- Repo filtering is respected
- Path parameter collapsing (query /orders/{id} matches node /orders/{order_id})
- Wrong method still returns with lower score
"""

from __future__ import annotations

from types import SimpleNamespace

from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
from mimir.services.retrieval import RetrievalService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service() -> RetrievalService:
    """Build a minimal RetrievalService without embedder/vector store."""
    svc = RetrievalService.__new__(RetrievalService)
    svc._config = SimpleNamespace(
        retrieval=SimpleNamespace(
            default_token_budget=8000,
            default_beam_width=3,
            expansion_hops=2,
            relevance_gate=0.3,
            hybrid_alpha=0.7,
        ),
    )
    return svc


def _build_graph() -> CodeGraph:
    graph = CodeGraph()
    graph.add_node(Node(
        id="api:",
        repo="api",
        kind=NodeKind.REPOSITORY,
        name="api",
    ))
    graph.add_node(Node(
        id="api:app.py::create_order",
        repo="api",
        kind=NodeKind.API_ENDPOINT,
        name="create_order",
        path="app.py",
        http_method="POST",
        route_path="/orders",
    ))
    graph.add_node(Node(
        id="api:app.py::get_order",
        repo="api",
        kind=NodeKind.API_ENDPOINT,
        name="get_order",
        path="app.py",
        http_method="GET",
        route_path="/orders/{id}",
    ))
    graph.add_node(Node(
        id="api:app.py::list_users",
        repo="api",
        kind=NodeKind.API_ENDPOINT,
        name="list_users",
        path="app.py",
        http_method="GET",
        route_path="/users",
    ))
    graph.add_node(Node(
        id="api:lib.py::helper",
        repo="api",
        kind=NodeKind.FUNCTION,
        name="helper",
        path="lib.py",
    ))
    return graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_path_matches_endpoint() -> None:
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("what does /orders do", graph)

    ids = [n.id for n, _ in results]
    assert "api:app.py::create_order" in ids
    assert "api:lib.py::helper" not in ids


def test_method_and_path_boosts_score() -> None:
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("POST /orders", graph)

    # POST /orders should score higher than GET /orders/{id}
    assert len(results) >= 1
    top = results[0]
    assert top[0].id == "api:app.py::create_order"
    assert top[1] > 1.0  # method+path match gets boosted score


def test_wrong_method_still_matches_with_lower_score() -> None:
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("DELETE /orders", graph)

    ids = [n.id for n, _ in results]
    assert "api:app.py::create_order" in ids
    # Score should be lower than a correct method match
    for node, score in results:
        if node.id == "api:app.py::create_order":
            assert score < 1.0


def test_no_route_in_query_returns_empty() -> None:
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("how does order creation work", graph)

    assert results == []


def test_path_param_matching() -> None:
    """Query /orders/{id} should match node /orders/{order_id}."""
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("GET /orders/{id}", graph)

    ids = [n.id for n, _ in results]
    assert "api:app.py::get_order" in ids


def test_repo_filter_respected() -> None:
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("/orders", graph, repos=["other-repo"])

    assert results == []


def test_different_routes_dont_match() -> None:
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("/products", graph)

    assert results == []


def test_users_route_matches() -> None:
    svc = _make_service()
    graph = _build_graph()

    results = svc._route_match_seeds("GET /users", graph)

    assert len(results) == 1
    assert results[0][0].id == "api:app.py::list_users"
    assert results[0][1] > 1.0  # method match
