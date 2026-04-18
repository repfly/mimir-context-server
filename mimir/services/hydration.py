"""Vector store hydration — populates the vector store from graph node embeddings.

Upserts only the delta: ids present in the graph but missing from the
store.  Persistent backends (Chroma) that already hold the HNSW index
on disk skip the work entirely on warm starts.  ``upsert`` is
idempotent in every backend, so the delta optimization is purely a
speedup — correctness does not depend on it.
"""

from __future__ import annotations

import logging

from mimir.ports.vector_store import VectorStore

logger = logging.getLogger(__name__)


def hydrate_vector_store(graph, vector_store: VectorStore) -> None:
    """Populate the vector store from graph node embeddings."""
    from mimir.services.indexing import IndexingService

    ids: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict] = []
    documents: list[str] = []

    for node in graph.all_nodes():
        if node.embedding:
            ids.append(node.id)
            embeddings.append(node.embedding)
            metadatas.append({
                "repo": node.repo,
                "kind": node.kind.value,
                "path": node.path or "",
                "last_modified": node.last_modified or "",
                "http_method": node.http_method or "",
                "route_path": node.route_path or "",
            })
            documents.append(IndexingService._embedding_text(node, graph))

    if not ids:
        return

    existing = vector_store.get_existing_ids(ids)
    if len(existing) == len(ids):
        logger.info(
            "Vector store up to date (%d embeddings) — skipping hydrate", len(ids),
        )
        return

    missing_indices = [i for i, vec_id in enumerate(ids) if vec_id not in existing]
    vector_store.upsert(
        ids=[ids[i] for i in missing_indices],
        embeddings=[embeddings[i] for i in missing_indices],
        metadatas=[metadatas[i] for i in missing_indices],
        documents=[documents[i] for i in missing_indices],
    )
    logger.info(
        "Hydrated vector store with %d new embeddings (%d already present)",
        len(missing_indices),
        len(existing),
    )
