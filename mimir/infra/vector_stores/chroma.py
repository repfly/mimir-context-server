"""ChromaDB-backed vector store for production use."""

from __future__ import annotations

import logging
from typing import Any, Optional

from mimir.domain.errors import StorageError
from mimir.ports.vector_store import VectorSearchResult

logger = logging.getLogger(__name__)


class ChromaVectorStore:
    """ChromaDB vector store with HNSW indexing and metadata filtering."""

    def __init__(self, persist_directory: Optional[str] = None, collection_name: str = "mimir") -> None:
        try:
            import chromadb
        except ImportError:
            raise StorageError(
                "ChromaDB is not installed. Install with: pip install chromadb"
            )

        try:
            if persist_directory:
                self._client = chromadb.PersistentClient(path=persist_directory)
            else:
                self._client = chromadb.Client()

            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "ChromaDB collection '%s' ready (%d vectors)",
                collection_name,
                self._collection.count(),
            )
        except Exception as exc:
            raise StorageError(f"Failed to initialise ChromaDB: {exc}") from exc

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: Optional[list[dict[str, Any]]] = None,
        documents: Optional[list[str]] = None,
    ) -> None:
        try:
            kwargs: dict[str, Any] = {"ids": ids, "embeddings": embeddings}
            if metadatas:
                # ChromaDB requires that all metadata values are str, int, float, or bool
                kwargs["metadatas"] = [
                    {k: v for k, v in m.items() if isinstance(v, (str, int, float, bool))}
                    for m in metadatas
                ]
            if documents:
                kwargs["documents"] = documents
            self._collection.upsert(**kwargs)
        except Exception as exc:
            raise StorageError(f"ChromaDB upsert failed: {exc}") from exc

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        where: Optional[dict[str, Any]] = None,
    ) -> list[VectorSearchResult]:
        try:
            kwargs: dict[str, Any] = {
                "query_embeddings": [query_embedding],
                "n_results": top_k,
            }
            if where:
                kwargs["where"] = where

            results = self._collection.query(**kwargs)

            output: list[VectorSearchResult] = []
            if results["ids"] and results["ids"][0]:
                ids = results["ids"][0]
                distances = results["distances"][0] if results["distances"] else [0.0] * len(ids)
                metadatas = results["metadatas"][0] if results["metadatas"] else [{}] * len(ids)

                for vec_id, dist, meta in zip(ids, distances, metadatas):
                    # ChromaDB cosine space returns distances in [0, 2].
                    # Convert to similarity in [0, 1]: sim = 1 - (dist / 2)
                    score = max(0.0, 1.0 - dist / 2.0)
                    output.append(VectorSearchResult(id=vec_id, score=score, metadata=meta or {}))

            return output
        except Exception as exc:
            raise StorageError(f"ChromaDB search failed: {exc}") from exc

    def delete(self, ids: list[str]) -> None:
        try:
            self._collection.delete(ids=ids)
        except Exception as exc:
            raise StorageError(f"ChromaDB delete failed: {exc}") from exc

    def get_existing_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        try:
            # include=[] skips embeddings/metadata/documents — we only need the ids back.
            result = self._collection.get(ids=ids, include=[])
            existing = result.get("ids") if isinstance(result, dict) else None
            return set(existing) if existing else set()
        except Exception as exc:
            raise StorageError(f"ChromaDB get_existing_ids failed: {exc}") from exc

    def count(self) -> int:
        return self._collection.count()

    def reset(self) -> None:
        try:
            self._client.delete_collection(self._collection.name)
            self._collection = self._client.get_or_create_collection(
                name=self._collection.name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:
            raise StorageError(f"ChromaDB reset failed: {exc}") from exc
