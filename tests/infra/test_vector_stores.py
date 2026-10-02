"""Contract tests for the VectorStore implementation.

Exercises NumpyVectorStore's upsert/search/delete/count/reset/get_existing_ids.
"""

from __future__ import annotations

import pytest

from mimir.infra.vector_stores.numpy_store import NumpyVectorStore
from mimir.ports.vector_store import VectorStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> VectorStore:
    return NumpyVectorStore()


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


def test_upsert_duplicate_id_in_one_batch_last_write_wins(store: VectorStore) -> None:
    store.upsert(
        ids=["x", "y", "x"],
        embeddings=[[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]],
        metadatas=[{"repo": "alpha"}, {"repo": "beta"}, {"repo": "gamma"}],
    )

    assert store.count() == 2
    results = store.search(query_embedding=[1.0, 0.0], top_k=5, where={"repo": "gamma"})
    assert [r.id for r in results] == ["x"]
    assert store.search(query_embedding=[1.0, 0.0], top_k=5, where={"repo": "alpha"}) == []


def test_upsert_mixed_update_and_append(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    store.upsert(
        ids=["c", "d"],
        embeddings=[[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 1.0, 0.0]],
        metadatas=[{"repo": "alpha"}, {"repo": "alpha"}],
    )

    assert store.count() == 4
    assert store.search(query_embedding=[0.0, 0.0, 0.0, 1.0], top_k=1)[0].id == "c"
    assert store.search(query_embedding=[0.0, 0.0, 1.0, 0.0], top_k=1)[0].id == "d"


def test_filtered_search_after_delete(store: VectorStore) -> None:
    ids, embeddings, metadatas = _sample_vectors()
    store.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
    store.search(query_embedding=[1.0, 0.0, 0.0, 0.0], top_k=5, where={"repo": "alpha"})

    store.delete(ids=["a"])

    results = store.search(query_embedding=[1.0, 0.0, 0.0, 0.0], top_k=5, where={"repo": "alpha"})
    assert [r.id for r in results] == ["c"]


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
    # embedded nodes.
    assert store.get_existing_ids([]) == set()
