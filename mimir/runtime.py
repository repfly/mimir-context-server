"""Runtime builders for indexing and query serving."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from dataclasses import dataclass
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mimir.domain.config import MimirConfig, SummaryMode, VectorBackend
from mimir.domain.errors import NoActiveIndexError, StorageError
from mimir.domain.graph import CodeGraph
from mimir.domain.index_state import IndexJob, IndexJobKind, IndexVersion
from mimir.infra.stores.sqlite_feedback import SqliteFeedbackStore
from mimir.infra.stores.sqlite_graph import SqliteGraphStore
from mimir.infra.stores.sqlite_index_jobs import SqliteIndexJobStore
from mimir.infra.stores.sqlite_index_metadata import SqliteIndexMetadataStore
from mimir.infra.stores.sqlite_session import SqliteSessionStore
from mimir.infra.vector_stores.numpy_store import NumpyVectorStore
from mimir.services.catalog import CatalogService
from mimir.services.feedback import FeedbackService
from mimir.services.agent_policy import AgentPolicyService
from mimir.services.guardrail import DiffAnalyzer, GuardrailService
from mimir.services.impact import ImpactService
from mimir.services.indexing import IndexingService
from mimir.services.quality import QualityService
from mimir.services.repo_sync import RepoSyncService
from mimir.services.retrieval import RetrievalService
from mimir.services.session import SessionService
from mimir.services.temporal import TemporalService
from mimir.services.write_context import WriteContextService

logger = logging.getLogger(__name__)


def _version_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def _index_root(config: MimirConfig) -> Path:
    return config.data_dir / "indexes"


def _config_hash(config: MimirConfig) -> str:
    def normalize(value):
        if is_dataclass(value):
            return {k: normalize(v) for k, v in asdict(value).items()}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(k): normalize(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value

    import json

    payload = json.dumps(normalize(config), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_embedder(config: MimirConfig):
    from mimir.infra.embedders.local import LocalEmbedder
    from mimir.infra.embedders.jina import JinaEmbedder

    model = config.embeddings.model
    if model.startswith("api:"):
        return JinaEmbedder(
            model=model.removeprefix("api:"),
            api_key_env=config.embeddings.api_key_env,
            batch_size=config.embeddings.batch_size,
        )
    model_name = model.removeprefix("local:") if model.startswith("local:") else model
    cache_dir = config.embeddings.cache_dir or str(config.session_dir / "models")
    return LocalEmbedder(model_name=model_name, cache_dir=cache_dir)


def _build_vector_store(config: MimirConfig):
    if config.vector_db.backend is VectorBackend.CHROMA:
        from mimir.infra.vector_stores.chroma import ChromaVectorStore

        return ChromaVectorStore(
            persist_directory=config.vector_db.persist_directory
            or str(config.session_dir / "chroma"),
        )
    return NumpyVectorStore()


def _embedding_dim(graph: CodeGraph) -> Optional[int]:
    for node in graph.all_nodes():
        if node.embedding:
            return len(node.embedding)
    return None


def _validate_graph_embeddings(graph: CodeGraph, expected_dim: Optional[int]) -> None:
    if expected_dim is None:
        return
    for node in graph.all_nodes():
        if node.embedding and len(node.embedding) != expected_dim:
            raise StorageError(
                f"Index embedding dimension mismatch for {node.id}: "
                f"expected {expected_dim}, got {len(node.embedding)}"
            )


@dataclass
class QueryView:
    version: IndexVersion
    graph: CodeGraph
    vector_store: NumpyVectorStore


class RuntimeFactory:
    """Build focused runtimes from a single config."""

    def __init__(self, config: MimirConfig) -> None:
        self.config = config

    def indexer(self) -> "IndexerRuntime":
        return IndexerRuntime(self.config)

    def query(self) -> "QueryRuntime":
        return QueryRuntime(self.config)


class IndexerRuntime:
    """Owns repo sync, parsing, embedding, and index publication."""

    def __init__(self, config: MimirConfig) -> None:
        self.config = config
        self.metadata_store = SqliteIndexMetadataStore(config.project_dir / "index_metadata.db")
        self.job_store = SqliteIndexJobStore(config.project_dir / "index_jobs.db")
        from mimir.infra.parsers.tree_sitter import TreeSitterParser

        self.parser = TreeSitterParser()
        self.embedder = _build_embedder(config)
        self.vector_store = _build_vector_store(config)
        self.graph_store = SqliteGraphStore(config.project_dir / "graph.db")
        self.indexing = IndexingService(
            config=config,
            parser=self.parser,
            embedder=self.embedder,
            vector_store=self.vector_store,
            graph_store=self.graph_store,
        )
        self.repo_sync = RepoSyncService(config)

    async def run_index(
        self,
        *,
        clean: bool = False,
        mode_override: Optional[SummaryMode | str] = None,
    ) -> IndexVersion:
        if clean:
            self.graph_store.clear()
            self.vector_store.reset()
            graph = await self.indexing.index_all(mode_override=mode_override)
        else:
            graph, _ = await self.indexing.index_incremental(mode_override=mode_override)
        return self.publish_graph(graph)

    async def sync_repo(
        self,
        repo_name: str,
        *,
        commit_sha: Optional[str] = None,
        mode_override: Optional[SummaryMode | str] = None,
    ) -> IndexVersion:
        await self.repo_sync.sync_repo(repo_name, commit_sha=commit_sha)
        graph = self._load_active_or_working_graph()
        self.graph_store.save(graph)
        await self.indexing.refresh_repo(
            graph,
            repo_name,
            mode_override=mode_override,
        )
        return self.publish_graph(graph)

    def enqueue(self, kind: IndexJobKind, *, repo: str | None = None, commit_sha: str | None = None) -> IndexJob:
        return self.job_store.enqueue(kind, repo=repo, commit_sha=commit_sha)

    async def worker_once(self) -> Optional[IndexJob]:
        job = self.job_store.claim_next()
        if job is None:
            return None
        try:
            if job.kind is IndexJobKind.FULL:
                version = await self.run_index(clean=True)
            elif job.kind is IndexJobKind.INCREMENTAL:
                version = await self.run_index(clean=False)
            elif job.kind is IndexJobKind.SYNC:
                if not job.repo:
                    raise ValueError("sync job requires repo")
                version = await self.sync_repo(job.repo, commit_sha=job.commit_sha)
            else:
                raise ValueError(f"Unknown index job kind: {job.kind}")
            self.job_store.complete(job.id, {"version": version.version})
        except Exception as exc:
            logger.exception("Index job failed: %s", job.id)
            self.job_store.fail(job.id, str(exc))
        return self.job_store.get(job.id)

    async def worker_loop(self, *, poll_interval: float = 5.0) -> None:
        while True:
            job = await self.worker_once()
            if job is None:
                await asyncio.sleep(poll_interval)

    def publish_graph(self, graph: CodeGraph) -> IndexVersion:
        version = _version_id()
        version_dir = _index_root(self.config) / version
        version_dir.mkdir(parents=True, exist_ok=True)
        graph_path = version_dir / "graph.db"
        store = SqliteGraphStore(graph_path)
        try:
            store.save(graph)
        finally:
            store.close()

        repo_commits = self.graph_store.get_all_repo_states()
        index_version = IndexVersion(
            version=version,
            graph_path=str(graph_path),
            node_count=graph.node_count,
            edge_count=graph.edge_count,
            repo_commits=repo_commits,
            embedding_model=self.config.embeddings.model,
            embedding_dim=_embedding_dim(graph),
            config_hash=_config_hash(self.config),
            created_by="mimir-indexer",
        )
        self.metadata_store.publish(index_version, activate=True)
        return self.metadata_store.get(version) or index_version

    def list_versions(self, *, limit: int = 20) -> list[IndexVersion]:
        return self.metadata_store.list_versions(limit=limit)

    def activate_version(self, version: str) -> IndexVersion:
        selected = self.metadata_store.get(version)
        if selected is None:
            raise ValueError(f"Unknown index version: {version}")
        graph_path = Path(selected.graph_path)
        if not graph_path.is_file():
            raise StorageError(f"Index graph not found: {graph_path}")
        activated = self.metadata_store.activate(version)
        if activated is None:
            raise ValueError(f"Unknown index version: {version}")
        return activated

    def prune_versions(self, *, keep: int = 5) -> list[str]:
        if keep <= 0:
            raise ValueError("keep must be positive")
        versions = self.metadata_store.list_versions(limit=10_000)
        active = self.metadata_store.get_active()
        active_id = active.version if active else None
        retained = {version.version for version in versions[:keep]}
        if active_id:
            retained.add(active_id)

        removed: list[str] = []
        for version in versions:
            if version.version in retained:
                continue
            graph_path = Path(version.graph_path)
            version_dir = graph_path.parent
            if version_dir.is_dir() and version_dir.parent == _index_root(self.config):
                shutil.rmtree(version_dir)
            self.metadata_store.delete(version.version)
            removed.append(version.version)
        return removed

    def _load_active_or_working_graph(self) -> CodeGraph:
        active = self.metadata_store.get_active()
        if active is not None:
            store = SqliteGraphStore(Path(active.graph_path))
            try:
                return store.load()
            finally:
                store.close()
        return self.graph_store.load()

    def close(self) -> None:
        self.graph_store.close()
        self.metadata_store.close()
        self.job_store.close()


class QueryRuntime:
    """Owns read-oriented query serving over the active published index."""

    def __init__(self, config: MimirConfig) -> None:
        self.config = config
        self.metadata_store = SqliteIndexMetadataStore(config.project_dir / "index_metadata.db")
        self.session_store = SqliteSessionStore(config.session_dir / "sessions.db")
        self.feedback_store = SqliteFeedbackStore(
            config.session_dir / "feedback.db",
            smoothing=config.feedback.score_smoothing,
        )
        self.quality = QualityService()
        self.temporal = TemporalService(config=config)
        self.write_context = WriteContextService()
        self.impact = ImpactService()
        from mimir.infra.parsers.tree_sitter import TreeSitterParser
        self.parser = TreeSitterParser()
        self.diff_analyzer = DiffAnalyzer(parser=self.parser)
        self.guardrail = GuardrailService(
            impact_service=self.impact,
            quality_service=self.quality,
            diff_analyzer=self.diff_analyzer,
        )
        self.agent_policy = AgentPolicyService(impact_service=self.impact)
        self.feedback = FeedbackService(config=config, feedback_store=self.feedback_store)
        self.session = SessionService(config=config, session_store=self.session_store)
        self.session.set_feedback_service(self.feedback)
        self.temporal.set_quality_service(self.quality)
        self.temporal.set_feedback_service(self.feedback)
        self.catalog = CatalogService(quality_service=self.quality, config=config)
        self._view: Optional[QueryView] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._embedder = None
        self._retrieval: Optional[RetrievalService] = None
        self._active_vector_store = NumpyVectorStore()
        self._load_active()

    @property
    def active_version(self) -> Optional[IndexVersion]:
        return self._view.version if self._view is not None else None

    @property
    def graph(self) -> CodeGraph:
        if self._view is None:
            return CodeGraph()
        return self._view.graph

    def require_active_version(self) -> IndexVersion:
        if self._view is None:
            raise NoActiveIndexError()
        return self._view.version

    def require_graph(self) -> CodeGraph:
        self.require_active_version()
        return self.graph

    @property
    def retrieval(self) -> RetrievalService:
        self.require_active_version()
        if self._retrieval is None:
            self._retrieval = self._build_retrieval(self._active_vector_store)
        return self._retrieval

    def _build_retrieval(self, vector_store: NumpyVectorStore) -> RetrievalService:
        if self._embedder is None:
            self._embedder = _build_embedder(self.config)
        return RetrievalService(
            config=self.config,
            embedder=self._embedder,
            vector_store=vector_store,
            quality_service=self.quality,
            temporal_service=self.temporal,
            graph_store=None,
            record_retrieval_metadata=False,
        )

    async def reload_if_changed(self) -> bool:
        active = self.metadata_store.get_active()
        current = self.active_version.version if self.active_version else None
        if active is None or active.version == current:
            return False
        self._load_version(active)
        return True

    def start_polling(self, *, interval: float = 10.0) -> None:
        if self._poll_task is not None:
            return

        async def _poll() -> None:
            while True:
                try:
                    await self.reload_if_changed()
                except Exception:
                    logger.warning("Index version polling failed", exc_info=True)
                await asyncio.sleep(interval)

        self._poll_task = asyncio.create_task(_poll())

    async def stop_polling(self) -> None:
        if self._poll_task is None:
            return
        self._poll_task.cancel()
        try:
            await self._poll_task
        except asyncio.CancelledError:
            pass
        self._poll_task = None

    def status(self) -> dict:
        version = self.active_version
        return {
            "active_version": version.version if version else None,
            "graph_nodes": self.graph.node_count,
            "graph_edges": self.graph.edge_count,
            "repo_commits": version.repo_commits if version else {},
            "schema_version": version.schema_version if version else None,
            "embedding_model": version.embedding_model if version else None,
            "embedding_dim": version.embedding_dim if version else None,
            "config_hash": version.config_hash if version else None,
            "created_by": version.created_by if version else None,
            "activated_at": version.activated_at if version else None,
        }

    def _load_active(self) -> None:
        active = self.metadata_store.get_active()
        if active is not None:
            self._load_version(active)

    def _load_version(self, version: IndexVersion) -> None:
        graph_path = Path(version.graph_path)
        if not graph_path.is_file():
            raise StorageError(f"Active index graph not found: {graph_path}")
        if version.schema_version != 1:
            raise StorageError(f"Unsupported index schema version: {version.schema_version}")
        if version.embedding_model and version.embedding_model != self.config.embeddings.model:
            raise StorageError(
                f"Active index embedding model mismatch: expected "
                f"{self.config.embeddings.model}, got {version.embedding_model}"
            )
        store = SqliteGraphStore(Path(version.graph_path))
        try:
            graph = store.load()
        finally:
            store.close()
        _validate_graph_embeddings(graph, version.embedding_dim)
        vector_store = NumpyVectorStore()
        from mimir.services.hydration import hydrate_vector_store

        hydrate_vector_store(graph, vector_store)
        self._view = QueryView(version=version, graph=graph, vector_store=vector_store)
        self._active_vector_store = vector_store
        self._retrieval = None

    def close(self) -> None:
        self.metadata_store.close()
        self.session_store.close()
        self.feedback_store.close()
