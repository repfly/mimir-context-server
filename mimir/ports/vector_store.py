"""VectorStore port — interface for similarity search over embeddings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class VectorSearchResult:
    """A single result from a vector store query."""

    id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    """Interface for vector similarity search.

    Implementations: ``ChromaVectorStore``, ``NumpyVectorStore``.
    """

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: Optional[list[dict[str, Any]]] = None,
        documents: Optional[list[str]] = None,
    ) -> None:
        """Insert or update vectors.

        Parameters
        ----------
        ids
            Unique identifiers for each vector.
        embeddings
            Dense vectors to store.
        metadatas
            Optional metadata dicts for each vector (used for filtering).
        documents
            Optional raw text for each vector (used for BM25 / keyword search).
        """
        ...

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        where: Optional[dict[str, Any]] = None,
    ) -> list[VectorSearchResult]:
        """Search for the top-k most similar vectors.

        Parameters
        ----------
        query_embedding
            The query vector.
        top_k
            Number of results to return.
        where
            Optional metadata filter (e.g. ``{"repo": "auth-service"}``).

        Returns
        -------
        list[VectorSearchResult]
            Results ordered by descending similarity.
        """
        ...

    def delete(self, ids: list[str]) -> None:
        """Remove vectors by ID."""
        ...

    def get_existing_ids(self, ids: list[str]) -> set[str]:
        """Return the subset of ``ids`` that already exist in the store.

        Used by the container's hydration path to skip re-upserting vectors
        that persistent backends (e.g. Chroma) already hold on disk.
        Implementations should probe only the given ids, not scan the whole
        store.
        """
        ...

    def count(self) -> int:
        """Return the total number of stored vectors."""
        ...

    def reset(self) -> None:
        """Delete all stored vectors."""
        ...
