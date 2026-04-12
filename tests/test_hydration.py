"""Tests for Container._hydrate_vector_store delta-upsert logic.

``_hydrate_vector_store`` should:
- skip entirely when the vector store already contains every embedded node id
- upsert only the missing ids when the store is partially populated
- upsert everything when the store is empty

We bypass ``Container.__init__`` (which has heavy infra side-effects) by
instantiating the class via ``__new__`` and injecting a stub vector store.
"""

from __future__ import annotations

from typing import Any, Optional

from mimir.container import Container
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
from mimir.ports.vector_store import VectorSearchResult


# ---------------------------------------------------------------------------
# Stub VectorStore — records upsert calls for assertion
# ---------------------------------------------------------------------------


class RecordingVectorStore:
    """Minimal VectorStore stub that records upsert calls."""

    def __init__(self, preloaded_ids: Optional[set[str]] = None) -> None:
        self._preloaded = set(preloaded_ids or [])
        self.upsert_calls: list[dict[str, Any]] = []

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: Optional[list[dict[str, Any]]] = None,
        documents: Optional[list[str]] = None,
    ) -> None:
        self.upsert_calls.append(
            {
                "ids": list(ids),
                "embeddings": list(embeddings),
                "metadatas": list(metadatas) if metadatas else [],
                "documents": list(documents) if documents else [],
            }
        )
        self._preloaded.update(ids)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        where: Optional[dict[str, Any]] = None,
    ) -> list[VectorSearchResult]:
        return []

    def delete(self, ids: list[str]) -> None:
        self._preloaded.difference_update(ids)

    def get_existing_ids(self, ids: list[str]) -> set[str]:
        return {i for i in ids if i in self._preloaded}

    def count(self) -> int:
        return len(self._preloaded)

    def reset(self) -> None:
        self._preloaded.clear()


# ---------------------------------------------------------------------------
# Graph fixture
# ---------------------------------------------------------------------------


def _build_graph_with_embedded_nodes(node_ids: list[str]) -> CodeGraph:
    """Build a graph where every given id has an embedding attached."""
    graph = CodeGraph()
    graph.add_node(Node(
        id="myrepo:",
        repo="myrepo",
        kind=NodeKind.REPOSITORY,
        name="myrepo",
    ))
    for i, node_id in enumerate(node_ids):
        graph.add_node(Node(
            id=node_id,
            repo="myrepo",
            kind=NodeKind.FUNCTION,
            name=f"fn_{i}",
            path="src/service.py",
            raw_code=f"def fn_{i}(): pass",
            embedding=[float(i), 0.0, 0.0, 0.0],
        ))
    return graph


def _hydrate(stub: RecordingVectorStore, graph: CodeGraph) -> None:
    """Invoke the hydration method on a Container instance without running __init__."""
    container = Container.__new__(Container)
    container.vector_store = stub
    container._hydrate_vector_store(graph)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hydrate_empty_store_upserts_everything() -> None:
    ids = ["myrepo:a", "myrepo:b", "myrepo:c"]
    graph = _build_graph_with_embedded_nodes(ids)
    stub = RecordingVectorStore(preloaded_ids=set())

    _hydrate(stub, graph)

    assert len(stub.upsert_calls) == 1
    assert set(stub.upsert_calls[0]["ids"]) == set(ids)


def test_hydrate_fully_populated_store_skips_upsert() -> None:
    ids = ["myrepo:a", "myrepo:b", "myrepo:c"]
    graph = _build_graph_with_embedded_nodes(ids)
    stub = RecordingVectorStore(preloaded_ids=set(ids))

    _hydrate(stub, graph)

    assert stub.upsert_calls == []


def test_hydrate_partially_populated_store_upserts_delta_only() -> None:
    ids = ["myrepo:a", "myrepo:b", "myrepo:c", "myrepo:d"]
    graph = _build_graph_with_embedded_nodes(ids)
    # Two ids already present; the other two should be upserted.
    stub = RecordingVectorStore(preloaded_ids={"myrepo:a", "myrepo:c"})

    _hydrate(stub, graph)

    assert len(stub.upsert_calls) == 1
    call = stub.upsert_calls[0]
    assert set(call["ids"]) == {"myrepo:b", "myrepo:d"}
    # Parallel lists must stay aligned: one embedding per id upserted.
    assert len(call["embeddings"]) == len(call["ids"])
    assert len(call["metadatas"]) == len(call["ids"])
    assert len(call["documents"]) == len(call["ids"])


def test_hydrate_preserves_input_order_in_delta() -> None:
    # Delta must be emitted in the same order we encountered missing ids in
    # graph traversal, so parallel lists stay index-aligned.
    ids = ["myrepo:a", "myrepo:b", "myrepo:c", "myrepo:d"]
    graph = _build_graph_with_embedded_nodes(ids)
    stub = RecordingVectorStore(preloaded_ids={"myrepo:b"})

    _hydrate(stub, graph)

    call = stub.upsert_calls[0]
    # "myrepo:b" is skipped; the remaining three appear in graph-walk order.
    assert call["ids"] == ["myrepo:a", "myrepo:c", "myrepo:d"]


def test_hydrate_no_embedded_nodes_is_noop() -> None:
    graph = CodeGraph()
    graph.add_node(Node(
        id="myrepo:",
        repo="myrepo",
        kind=NodeKind.REPOSITORY,
        name="myrepo",
    ))
    # Node without embedding is skipped entirely.
    graph.add_node(Node(
        id="myrepo:unembedded",
        repo="myrepo",
        kind=NodeKind.FUNCTION,
        name="unembedded",
    ))
    stub = RecordingVectorStore()

    _hydrate(stub, graph)

    assert stub.upsert_calls == []
