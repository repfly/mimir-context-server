"""Dependency injection container.

Assembles the full object graph from configuration at startup.
No global state, no singletons, no module-level side effects.
"""

from __future__ import annotations

import logging
from typing import Optional

from mimir.domain.config import MimirConfig

logger = logging.getLogger(__name__)


class Container:
    """Wires all layers together from a ``MimirConfig``."""

    def __init__(self, config: MimirConfig) -> None:
        self.config = config
        self._graph = None  # lazy loaded
        self._watcher = None  # lazy, created on demand

        # Infrastructure ------------------------------------------------

        # Parser
        from mimir.infra.parsers.tree_sitter import TreeSitterParser
        self.parser = TreeSitterParser()

        # Embedder
        self.embedder = self._build_embedder()

        # Vector store
        self.vector_store = self._build_vector_store()

        # Graph store
        from mimir.infra.stores.sqlite_graph import SqliteGraphStore
        self.graph_store = SqliteGraphStore(config.data_dir / "graph.db")

        # Session store
        from mimir.infra.stores.sqlite_session import SqliteSessionStore
        self.session_store = SqliteSessionStore(config.data_dir / "sessions.db")

        # LLM client (used by the `ask` CLI command for interactive Q&A)
        self.llm_client = self._build_llm_client()

        # Services ------------------------------------------------------

        from mimir.services.indexing import IndexingService
        self.indexing = IndexingService(
            config=config,
            parser=self.parser,
            embedder=self.embedder,
            vector_store=self.vector_store,
            graph_store=self.graph_store,
        )

        from mimir.services.retrieval import RetrievalService
        self.retrieval = RetrievalService(
            config=config,
            embedder=self.embedder,
            vector_store=self.vector_store,
        )

        from mimir.services.temporal import TemporalService
        self.temporal = TemporalService(config=config)

        from mimir.services.write_context import WriteContextService
        self.write_context = WriteContextService()

        from mimir.services.impact import ImpactService
        self.impact = ImpactService()

        from mimir.services.session import SessionService
        self.session = SessionService(
            config=config,
            session_store=self.session_store,
        )

        from mimir.services.quality import QualityService
        self.quality = QualityService()
        self.temporal.set_quality_service(self.quality)
        self.retrieval.set_quality_service(self.quality)

    def _build_embedder(self):
        model = self.config.embeddings.model
        if model.startswith("api:"):
            # Explicit API mode — use Jina HTTP API
            from mimir.infra.embedders.jina import JinaEmbedder
            api_model = model.removeprefix("api:")
            try:
                return JinaEmbedder(
                    model=api_model,
                    api_key_env=self.config.embeddings.api_key_env,
                    batch_size=self.config.embeddings.batch_size,
                )
            except Exception as exc:
                logger.warning("Jina API embedder init failed (%s), falling back to local", exc)
                # Default: run locally via sentence-transformers
                from mimir.infra.embedders.local import LocalEmbedder
                model_name = model.removeprefix("local:") if model.startswith("local:") else model
                cache_dir = self.config.embeddings.cache_dir or str(self.config.data_dir / "models")
                logger.info("Using local embedder: %s (cache: %s)", model_name, cache_dir)
                return LocalEmbedder(model_name=model_name, cache_dir=cache_dir)
        else:
            # Default: run locally via sentence-transformers
            from mimir.infra.embedders.local import LocalEmbedder
            model_name = model.removeprefix("local:") if model.startswith("local:") else model
            cache_dir = self.config.embeddings.cache_dir or str(self.config.data_dir / "models")
            logger.info("Using local embedder: %s (cache: %s)", model_name, cache_dir)
            return LocalEmbedder(model_name=model_name, cache_dir=cache_dir)

    def _build_vector_store(self):
        backend = self.config.vector_db.backend
        if backend == "chroma":
            from mimir.infra.vector_stores.chroma import ChromaVectorStore
            return ChromaVectorStore(
                persist_directory=self.config.vector_db.persist_directory
                or str(self.config.data_dir / "chroma"),
            )
        else:
            from mimir.infra.vector_stores.numpy_store import NumpyVectorStore
            return NumpyVectorStore()

    def _build_llm_client(self):
        from mimir.infra.llm.litellm_client import LiteLlmClient
        return LiteLlmClient(
            model=self.config.llm.model,
            max_concurrent=self.config.indexing.concurrency,
            api_base=self.config.llm.api_base,
        )

    def load_graph(self):
        """Load the persisted graph and hydrate the vector store."""
        if self._graph is None:
            self._graph = self.graph_store.load()
            self._hydrate_vector_store(self._graph)
        return self._graph

    def _hydrate_vector_store(self, graph) -> None:
        """Populate the in-memory vector store from graph node embeddings."""
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
                })
                documents.append(IndexingService._embedding_text(node, graph))

        if ids:
            self.vector_store.upsert(
                ids=ids, embeddings=embeddings,
                metadatas=metadatas, documents=documents,
            )
            logger.info("Hydrated vector store with %d embeddings", len(ids))

    def clear_data(self, *, graph: bool = True, sessions: bool = True) -> dict:
        """Delete all locally stored data.

        Parameters
        ----------
        graph:
            Clear the code graph, embeddings, and repo state.
        sessions:
            Clear all conversation sessions.

        Returns a summary of what was cleared.
        """
        import shutil
        from pathlib import Path

        cleared: list[str] = []

        if graph:
            self.graph_store.clear()
            self._graph = None  # invalidate in-memory cache
            cleared.append("graph")

            # Reset the vector store through its live client so file handles stay valid.
            # We must NOT use shutil.rmtree() here while the store's SQLite client is open,
            # because deleting chroma files under an active handle puts SQLite into readonly mode.
            self.vector_store.reset()
            cleared.append("vector_store")

        if sessions:
            self.session_store.clear()
            cleared.append("sessions")

        logger.info("Data cleared: %s", cleared)
        return {"cleared": cleared}

    @property
    def watcher(self):
        """Lazy-create the file watcher service."""
        if self._watcher is None:
            from mimir.services.watcher import FileWatcherService
            self._watcher = FileWatcherService(
                config=self.config,
                indexing_service=self.indexing,
                graph=self.load_graph(),
                graph_store=self.graph_store,
                vector_store=self.vector_store,
                retrieval_service=self.retrieval,
            )
        return self._watcher

    def warmup(self) -> None:
        """Eagerly load the embedding model so the first query is fast."""
        if hasattr(self.embedder, '_ensure_model'):
            logger.info("Warming up embedding model…")
            self.embedder._ensure_model()
            logger.info("Embedding model ready.")

    def close(self) -> None:
        """Release all resources."""
        if self._watcher is not None:
            self._watcher.stop()
        self.graph_store.close()
        self.session_store.close()
