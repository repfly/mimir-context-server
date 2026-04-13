"""NumPy-based in-memory vector store.

Lightweight alternative to ChromaDB for development and small codebases.
Stores all vectors in memory using NumPy arrays.  Supports metadata
filtering and cosine similarity search.
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

        metas = metadatas or [{}] * len(ids)
        docs = documents or [None] * len(ids)
        new_vecs = np.array(embeddings, dtype=np.float32)

        for i, vec_id in enumerate(ids):
            if vec_id in self._id_to_idx:
                # Update in-place
                idx = self._id_to_idx[vec_id]
                if self._embeddings is not None:
                    self._embeddings[idx] = new_vecs[i]
                self._metadatas[idx] = metas[i]
                self._documents[idx] = docs[i]
            else:
                # Append
                idx = len(self._ids)
                self._ids.append(vec_id)
                self._metadatas.append(metas[i])
                self._documents.append(docs[i])
                self._id_to_idx[vec_id] = idx

                if self._embeddings is None:
                    self._embeddings = new_vecs[i : i + 1]
                else:
                    self._embeddings = np.vstack([self._embeddings, new_vecs[i : i + 1]])

        logger.debug("Upserted %d vectors (total: %d)", len(ids), len(self._ids))

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        where: Optional[dict[str, Any]] = None,
    ) -> list[VectorSearchResult]:
        if self._embeddings is None or len(self._ids) == 0:
            return []

        query = np.array(query_embedding, dtype=np.float32)

        # Cosine similarity
        norms = np.linalg.norm(self._embeddings, axis=1)
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return []

        similarities = self._embeddings @ query / (norms * query_norm + 1e-10)

        # Apply metadata filter
        if where:
            mask = np.ones(len(self._ids), dtype=bool)
            for key, value in where.items():
                for i, meta in enumerate(self._metadatas):
                    if meta.get(key) != value:
                        mask[i] = False
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
