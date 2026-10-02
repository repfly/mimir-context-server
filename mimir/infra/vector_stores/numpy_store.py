"""NumPy-based in-memory vector store.

Stores all vectors in memory using NumPy arrays and is rebuilt from the
embeddings in graph.db on startup.  Supports metadata filtering and exact
(brute-force) cosine similarity search.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from mimir.domain.errors import StorageError
from mimir.ports.vector_store import VectorSearchResult

logger = logging.getLogger(__name__)


class NumpyVectorStore:
    """In-memory vector store backed by NumPy arrays."""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._embeddings: Optional[np.ndarray] = None  # shape: (n, dim)
        self._metadatas: list[dict[str, Any]] = []
        self._documents: list[Optional[str]] = []
        self._id_to_idx: dict[str, int] = {}
        # Derived caches, invalidated on every mutation
        self._unit: Optional[np.ndarray] = None  # row-normalized embeddings
        self._columns: dict[str, np.ndarray] = {}  # metadata key -> values per row

    def _invalidate(self) -> None:
        self._unit = None
        self._columns.clear()

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: Optional[list[dict[str, Any]]] = None,
        documents: Optional[list[str]] = None,
    ) -> None:
        if len(ids) != len(embeddings):
            raise StorageError(
                f"ids ({len(ids)}) and embeddings ({len(embeddings)}) length mismatch"
            )
        if not ids:
            return

        metas = metadatas or [{}] * len(ids)
        docs = documents or [None] * len(ids)
        new_vecs = np.asarray(embeddings, dtype=np.float32)

        # Updates are written in place; appends are collected and stacked once
        # (row-by-row vstack would make bulk loads quadratic).
        update_rows: list[int] = []
        update_idx: list[int] = []
        append_rows: list[int] = []
        for i, vec_id in enumerate(ids):
            idx = self._id_to_idx.get(vec_id)
            if idx is None:
                idx = len(self._ids)
                self._ids.append(vec_id)
                self._metadatas.append(metas[i])
                self._documents.append(docs[i])
                self._id_to_idx[vec_id] = idx
                append_rows.append(i)
            else:
                self._metadatas[idx] = metas[i]
                self._documents[idx] = docs[i]
                if idx < self._stored_rows():
                    update_idx.append(idx)
                    update_rows.append(i)
                else:
                    # Duplicate id earlier in this same batch — last write wins
                    append_rows[idx - self._stored_rows()] = i

        if update_idx:
            self._embeddings[update_idx] = new_vecs[update_rows]  # type: ignore[index]
        if append_rows:
            added = new_vecs[append_rows]
            self._embeddings = added if self._embeddings is None else np.vstack([self._embeddings, added])

        self._invalidate()
        logger.debug("Upserted %d vectors (total: %d)", len(ids), len(self._ids))

    def _stored_rows(self) -> int:
        return 0 if self._embeddings is None else len(self._embeddings)

    def _column(self, key: str) -> np.ndarray:
        col = self._columns.get(key)
        if col is None:
            col = np.empty(len(self._metadatas), dtype=object)
            col[:] = [m.get(key) for m in self._metadatas]
            self._columns[key] = col
        return col

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        where: Optional[dict[str, Any]] = None,
    ) -> list[VectorSearchResult]:
        if self._embeddings is None or len(self._ids) == 0:
            return []

        query = np.asarray(query_embedding, dtype=np.float32)
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return []

        # Cosine similarity against cached unit vectors
        if self._unit is None:
            norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
            self._unit = self._embeddings / (norms + 1e-10)
        similarities = self._unit @ (query / query_norm)

        # Apply metadata filter
        if where:
            mask = np.ones(len(self._ids), dtype=bool)
            for key, value in where.items():
                mask &= self._column(key) == value
            similarities = np.where(mask, similarities, -np.inf)

        # Top-k
        k = min(top_k, len(self._ids))
        top_indices = np.argpartition(similarities, -k)[-k:]
        top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

        results: list[VectorSearchResult] = []
        for idx in top_indices:
            score = float(similarities[idx])
            if score == float("-inf"):
                continue
            results.append(VectorSearchResult(
                id=self._ids[idx],
                score=score,
                metadata=self._metadatas[idx],
            ))

        return results

    def delete(self, ids: list[str]) -> None:
        indices_to_remove = sorted(
            [self._id_to_idx[i] for i in ids if i in self._id_to_idx],
            reverse=True,
        )
        for idx in indices_to_remove:
            self._ids.pop(idx)
            self._metadatas.pop(idx)
            self._documents.pop(idx)

        if indices_to_remove and self._embeddings is not None:
            mask = np.ones(len(self._embeddings), dtype=bool)
            for idx in indices_to_remove:
                if idx < len(mask):
                    mask[idx] = False
            self._embeddings = self._embeddings[mask] if mask.any() else None

        # Rebuild index
        self._id_to_idx = {vid: i for i, vid in enumerate(self._ids)}
        self._invalidate()

    def get_existing_ids(self, ids: list[str]) -> set[str]:
        return {i for i in ids if i in self._id_to_idx}

    def count(self) -> int:
        return len(self._ids)

    def reset(self) -> None:
        self._ids.clear()
        self._embeddings = None
        self._metadatas.clear()
        self._documents.clear()
        self._id_to_idx.clear()
        self._invalidate()
