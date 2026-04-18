"""Parity tests for VectorStore implementations.

Exercises NumpyVectorStore and ChromaVectorStore against the same fixtures to
ensure they agree on upsert/search/delete/count/reset/get_existing_ids.
Metadata is intentionally scalar-only — ChromaVectorStore silently drops
non-scalar metadata values (see ``chroma.py``), which would masquerade as a
parity failure otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimir.infra.vector_stores.chroma import ChromaVectorStore
from mimir.infra.vector_stores.numpy_store import NumpyVectorStore
from mimir.ports.vector_store import VectorStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _numpy_store() -> VectorStore:
    return NumpyVectorStore()


def _chroma_store(tmp_path: Path) -> VectorStore:
    # Use a unique sub-path so parallel test runs don't collide.
    return ChromaVectorStore(persist_directory=str(tmp_path / "chroma"))


@pytest.fixture(params=["numpy", "chroma"])
def store(request, tmp_path: Path) -> VectorStore:
    if request.param == "numpy":
        return _numpy_store()
    return _chroma_store(tmp_path)


def _sample_vectors() -> tuple[list[str], list[list[float]], list[dict]]:
    ids = ["a", "b", "c"]
    # 4-D vectors along distinct axes so ranking is unambiguous.
    embeddings = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
    ]
    metadatas = [
        {"repo": "alpha", "kind": "function"},
        {"repo": "beta", "kind": "function"},
        {"repo": "alpha", "kind": "class"},
    ]
    return ids, embeddings, metadatas


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_store_implements_protocol(store: VectorStore) -> None:
    assert isinstance(store, VectorStore)


# ---------------------------------------------------------------------------
# upsert / search
# ---------------------------------------------------------------------------


def test_upsert_then_search_returns_nearest_first(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    results = store.search(query_embedding=[1.0, 0.0, 0.0, 0.0], top_k=3)

    assert len(results) == 3
    assert results[0].id == "a"  # exact match on first axis
    # Scores are descending.
    assert results[0].score >= results[1].score >= results[2].score


def test_search_top_k_is_respected(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    results = store.search(query_embedding=[1.0, 0.0, 0.0, 0.0], top_k=1)

    assert len(results) == 1
    assert results[0].id == "a"


def test_search_with_metadata_filter(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    results = store.search(
        query_embedding=[1.0, 0.0, 0.0, 0.0],
        top_k=5,
        where={"repo": "beta"},
    )

    assert len(results) == 1
    assert results[0].id == "b"


def test_upsert_updates_existing_id(store: VectorStore) -> None:
    store.upsert(ids=["x"], embeddings=[[1.0, 0.0]], metadatas=[{"repo": "alpha"}])
    store.upsert(ids=["x"], embeddings=[[0.0, 1.0]], metadatas=[{"repo": "alpha"}])

    # Updated vector should be nearest to [0, 1], not [1, 0].
    results = store.search(query_embedding=[0.0, 1.0], top_k=1)

    assert len(results) == 1
    assert results[0].id == "x"
    assert store.count() == 1


# ---------------------------------------------------------------------------
# delete / count / reset
# ---------------------------------------------------------------------------


def test_delete_removes_vectors(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
    assert store.count() == 3

    store.delete(ids=["b"])

    assert store.count() == 2
    assert store.get_existing_ids(["a", "b", "c"]) == {"a", "c"}


def test_reset_empties_the_store(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    store.reset()

    assert store.count() == 0
    assert store.get_existing_ids(ids) == set()


# ---------------------------------------------------------------------------
# get_existing_ids
# ---------------------------------------------------------------------------


def test_get_existing_ids_empty_store(store: VectorStore) -> None:
    assert store.get_existing_ids(["a", "b", "c"]) == set()


def test_get_existing_ids_partial_match(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    assert store.get_existing_ids(["a", "z", "c"]) == {"a", "c"}


def test_get_existing_ids_all_present(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    assert store.get_existing_ids(ids) == set(ids)


def test_get_existing_ids_empty_input(store: VectorStore) -> None:
    # Edge case: callers may pass an empty id list when the graph has no
    # embedded nodes.  Both backends must handle this without hitting the
    # underlying collection.
    assert store.get_existing_ids([]) == set()
