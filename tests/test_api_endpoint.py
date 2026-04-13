"""Tests for API_ENDPOINT node fixes.

Covers:
- API_ENDPOINT membership in SYMBOL_KINDS
- Node.is_symbol returns True for API_ENDPOINT
- symbol_nodes() yields API_ENDPOINT nodes
- http_method / route_path fields round-trip through to_dict / from_dict
- Backward compatibility when old data lacks route fields
- _embedding_text includes route info for API_ENDPOINT nodes
"""

from __future__ import annotations

from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind, SYMBOL_KINDS


# ---------------------------------------------------------------------------
# SYMBOL_KINDS membership
# ---------------------------------------------------------------------------


def test_api_endpoint_in_symbol_kinds() -> None:
    assert NodeKind.API_ENDPOINT in SYMBOL_KINDS


def test_api_endpoint_is_symbol() -> None:
    node = Node(
        id="repo:path::handler",
        repo="repo",
        kind=NodeKind.API_ENDPOINT,
        name="handler",
    )
    assert node.is_symbol is True


def test_symbol_nodes_yields_api_endpoint() -> None:
    graph = CodeGraph()
    graph.add_node(Node(
        id="repo:",
        repo="repo",
        kind=NodeKind.REPOSITORY,
        name="repo",
    ))
    ep = Node(
        id="repo:app.py::create_order",
        repo="repo",
        kind=NodeKind.API_ENDPOINT,
        name="create_order",
        http_method="POST",
        route_path="/orders",
    )
    graph.add_node(ep)

    symbols = list(graph.symbol_nodes())
    assert any(n.id == ep.id for n in symbols)


# ---------------------------------------------------------------------------
# Node route field serialization
# ---------------------------------------------------------------------------


def test_node_route_fields_roundtrip() -> None:
    node = Node(
        id="repo:app.py::get_user",
        repo="repo",
        kind=NodeKind.API_ENDPOINT,
        name="get_user",
        http_method="GET",
        route_path="/users/{id}",
    )
    d = node.to_dict()
    assert d["http_method"] == "GET"
    assert d["route_path"] == "/users/{id}"

    restored = Node.from_dict(d)
    assert restored.http_method == "GET"
    assert restored.route_path == "/users/{id}"
    assert restored.kind == NodeKind.API_ENDPOINT


def test_node_from_dict_missing_route_fields() -> None:
    """Old serialized data without http_method/route_path should produce None."""
    d = {
        "id": "repo:app.py::handler",
        "repo": "repo",
        "kind": "api_endpoint",
        "name": "handler",
    }
    node = Node.from_dict(d)
    assert node.http_method is None
    assert node.route_path is None
    assert node.kind == NodeKind.API_ENDPOINT


# ---------------------------------------------------------------------------
# Embedding text augmentation
# ---------------------------------------------------------------------------


def test_embedding_text_includes_route() -> None:
    from mimir.services.indexing import IndexingService

    graph = CodeGraph()
    node = Node(
        id="repo:app.py::create_order",
        repo="repo",
        kind=NodeKind.API_ENDPOINT,
        name="create_order",
        path="app.py",
        raw_code='@app.post("/orders")\ndef create_order(): pass',
        http_method="POST",
        route_path="/orders",
    )
    graph.add_node(node)

    text = IndexingService._embedding_text(node, graph)

    assert "Route: POST /orders" in text
    assert "File: app.py" in text


def test_embedding_text_omits_route_when_absent() -> None:
    from mimir.services.indexing import IndexingService

    graph = CodeGraph()
    node = Node(
        id="repo:lib.py::helper",
        repo="repo",
        kind=NodeKind.FUNCTION,
        name="helper",
        path="lib.py",
        raw_code="def helper(): pass",
    )
    graph.add_node(node)

    text = IndexingService._embedding_text(node, graph)

    assert "Route:" not in text
