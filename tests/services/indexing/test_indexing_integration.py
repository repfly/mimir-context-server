"""Integration test for the core indexing pipeline.

Exercises the real end-to-end flow:
  temp Python repo on disk → TreeSitter parser → graph builder →
  cross-file resolution → summarisation → embedding → vector upsert →
  graph persistence → reload.

Uses real infra (TreeSitter, NumpyVectorStore, SqliteGraphStore) and a
lightweight embedder stub to avoid heavy model downloads.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from mimir.domain.config import (
    CrossRepoConfig,
    EmbeddingConfig,
    IndexingConfig,
    MimirConfig,
    RepoConfig,
    VectorDbConfig,
)
from mimir.domain.graph import CodeGraph
from mimir.domain.models import EdgeKind, NodeKind, SYMBOL_KINDS
from mimir.infra.stores.sqlite_graph import SqliteGraphStore
from mimir.infra.vector_stores.numpy_store import NumpyVectorStore
from mimir.services.indexing import IndexingService


# ---------------------------------------------------------------------------
# Lightweight embedder stub — deterministic, no model download
# ---------------------------------------------------------------------------

_EMBED_DIM = 8


class _DeterministicEmbedder:
    """Produces deterministic vectors from text hashes. No model needed."""

    @property
    def dimension(self) -> int:
        return _EMBED_DIM

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        result: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vec = [float(b) / 255.0 for b in digest[:_EMBED_DIM]]
            result.append(vec)
        return result


# ---------------------------------------------------------------------------
# Fixture: create a mini Python repo on disk
# ---------------------------------------------------------------------------

_MODELS_PY = textwrap.dedent("""\
    class User:
        \"\"\"Domain model for a user.\"\"\"

        def __init__(self, name: str, email: str):
            self.name = name
            self.email = email

        def display_name(self) -> str:
            return self.name.title()


    class UserNotFoundError(Exception):
        pass
""")

_SERVICE_PY = textwrap.dedent("""\
    from models import User, UserNotFoundError


    class UserService:
        \"\"\"Application service for user operations.\"\"\"

        def __init__(self, repo):
            self.repo = repo

        def get_user(self, user_id: int) -> User:
            user = self.repo.find(user_id)
            if user is None:
                raise UserNotFoundError(f"User {user_id} not found")
            return user

        def list_users(self) -> list:
            return self.repo.all()
""")

_ROUTES_PY = textwrap.dedent("""\
    from service import UserService


    def get_user_handler(request):
        svc = UserService(request.app["repo"])
        user = svc.get_user(request.match_info["id"])
        return {"name": user.display_name()}

    def list_users_handler(request):
        svc = UserService(request.app["repo"])
        return [u.display_name() for u in svc.list_users()]
""")


@pytest.fixture()
def repo_on_disk(tmp_path: Path) -> Path:
    """Write a small Python project to a temp directory."""
    repo = tmp_path / "my_app"
    repo.mkdir()
    (repo / "models.py").write_text(_MODELS_PY)
    (repo / "service.py").write_text(_SERVICE_PY)
    (repo / "routes.py").write_text(_ROUTES_PY)
    return repo


@pytest.fixture()
def mimir_config(tmp_path: Path, repo_on_disk: Path) -> MimirConfig:
    data_dir = tmp_path / ".mimir"
    return MimirConfig(
        repos=[
            RepoConfig(name="my_app", path=repo_on_disk, language_hint="python"),
        ],
        data_dir=data_dir,
        indexing=IndexingConfig(
            summary_mode="heuristic",
            excluded_patterns=["__pycache__", "*.pyc"],
            max_file_size_kb=500,
        ),
        cross_repo=CrossRepoConfig(
            detect_api_contracts=True,
            detect_shared_imports=True,
        ),
        embeddings=EmbeddingConfig(batch_size=64),
        vector_db=VectorDbConfig(backend="numpy"),
    )


@pytest.fixture()
def graph_store(mimir_config: MimirConfig) -> SqliteGraphStore:
    store = SqliteGraphStore(mimir_config.project_dir / "graph.db")
    yield store
    store.close()


@pytest.fixture()
def vector_store() -> NumpyVectorStore:
    return NumpyVectorStore()


@pytest.fixture()
def indexing_service(
    mimir_config: MimirConfig,
    graph_store: SqliteGraphStore,
    vector_store: NumpyVectorStore,
) -> IndexingService:
    from mimir.infra.parsers.tree_sitter import TreeSitterParser

    return IndexingService(
        config=mimir_config,
        parser=TreeSitterParser(),
        embedder=_DeterministicEmbedder(),
        vector_store=vector_store,
        graph_store=graph_store,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFullIndexPipeline:
    """End-to-end: index_all → graph assertions."""

    @pytest.mark.asyncio
    async def test_index_builds_complete_graph(
        self, indexing_service: IndexingService,
    ) -> None:
        graph = await indexing_service.index_all()

        # Repository root node exists
        assert graph.has_node("my_app:")
        repo_node = graph.get_node("my_app:")
        assert repo_node.kind == NodeKind.REPOSITORY

        # File nodes exist
        assert graph.has_node("my_app:models.py")
        assert graph.has_node("my_app:service.py")
        assert graph.has_node("my_app:routes.py")
        for file_id in ("my_app:models.py", "my_app:service.py", "my_app:routes.py"):
            assert graph.get_node(file_id).kind == NodeKind.FILE

    @pytest.mark.asyncio
    async def test_index_extracts_symbols(
        self, indexing_service: IndexingService,
    ) -> None:
        graph = await indexing_service.index_all()

        # Class symbols
        class_nodes = [n for n in graph.all_nodes() if n.kind == NodeKind.CLASS]
        class_names = {n.name for n in class_nodes}
        assert "User" in class_names
        assert "UserService" in class_names
        assert "UserNotFoundError" in class_names

        # Function/method symbols
        func_names = {
            n.name
            for n in graph.all_nodes()
            if n.kind in (NodeKind.FUNCTION, NodeKind.METHOD)
        }
        assert "get_user" in func_names or "get_user_handler" in func_names

    @pytest.mark.asyncio
    async def test_index_creates_containment_edges(
        self, indexing_service: IndexingService,
    ) -> None:
        graph = await indexing_service.index_all()

        # Repo → file containment
        repo_children = graph.get_children("my_app:")
        child_ids = {c.id for c in repo_children}
        assert "my_app:models.py" in child_ids

        # File → symbol containment
        models_children = graph.get_children("my_app:models.py")
        child_names = {c.name for c in models_children}
        assert "User" in child_names

    @pytest.mark.asyncio
    async def test_index_resolves_cross_file_refs(
        self, indexing_service: IndexingService,
    ) -> None:
        graph = await indexing_service.index_all()

        # Service references User — there should be a CALLS or USES_TYPE edge
        cross_file_edge_kinds = {EdgeKind.CALLS, EdgeKind.USES_TYPE, EdgeKind.INHERITS}
        cross_file_edges = [
            e for e in graph.all_edges() if e.kind in cross_file_edge_kinds
        ]
        # At minimum, the cross-file linker should have found some references
        # between service.py and models.py
        assert len(cross_file_edges) > 0, (
            "Expected cross-file reference edges between service.py and models.py"
        )

    @pytest.mark.asyncio
    async def test_index_generates_summaries(
        self, indexing_service: IndexingService,
    ) -> None:
        graph = await indexing_service.index_all()

        # In heuristic mode, file-level nodes should get summaries
        files_with_summaries = [
            n for n in graph.all_nodes()
            if n.kind == NodeKind.FILE and n.summary
        ]
        assert len(files_with_summaries) > 0

    @pytest.mark.asyncio
    async def test_index_embeds_all_eligible_nodes(
        self, indexing_service: IndexingService, vector_store: NumpyVectorStore,
    ) -> None:
        graph = await indexing_service.index_all()

        # Every symbol node should have an embedding
        symbol_nodes = [n for n in graph.all_nodes() if n.kind in SYMBOL_KINDS]
        assert len(symbol_nodes) > 0
        for node in symbol_nodes:
            assert node.embedding is not None, f"Symbol {node.id} has no embedding"
            assert len(node.embedding) == _EMBED_DIM

        # Vector store should contain all embedded nodes
        stored_count = vector_store.count()
        embedded_count = sum(1 for n in graph.all_nodes() if n.embedding)
        assert stored_count == embedded_count


class TestGraphPersistence:
    """Index → persist → reload → verify round-trip."""

    @pytest.mark.asyncio
    async def test_graph_survives_save_load_cycle(
        self,
        indexing_service: IndexingService,
        graph_store: SqliteGraphStore,
    ) -> None:
        original = await indexing_service.index_all()

        # Graph was saved by index_all — reload from store
        reloaded = graph_store.load()

        assert reloaded.node_count == original.node_count
        assert reloaded.edge_count == original.edge_count

        # Spot-check a few nodes
        for node_id in ("my_app:", "my_app:models.py"):
            orig_node = original.get_node(node_id)
            reload_node = reloaded.get_node(node_id)
            assert reload_node is not None, f"Node {node_id} missing after reload"
            assert reload_node.kind == orig_node.kind
            assert reload_node.name == orig_node.name

    @pytest.mark.asyncio
    async def test_embeddings_survive_save_load(
        self,
        indexing_service: IndexingService,
        graph_store: SqliteGraphStore,
    ) -> None:
        original = await indexing_service.index_all()
        reloaded = graph_store.load()

        original_embedded = {
            n.id: n.embedding for n in original.all_nodes() if n.embedding
        }
        for node_id, orig_emb in original_embedded.items():
            reload_node = reloaded.get_node(node_id)
            assert reload_node is not None
            assert reload_node.embedding is not None, (
                f"Embedding lost for {node_id} after reload"
            )
            assert reload_node.embedding == pytest.approx(orig_emb, abs=1e-5)


class TestIncrementalIndex:
    """Index → modify file → incremental re-index → verify delta."""

    @pytest.mark.asyncio
    async def test_incremental_after_file_change(
        self,
        indexing_service: IndexingService,
        repo_on_disk: Path,
        vector_store: NumpyVectorStore,
    ) -> None:
        # Full index first
        original = await indexing_service.index_all()
        original_node_count = original.node_count

        # Simulate an incremental update via index_files
        new_content = textwrap.dedent("""\
            class AdminService:
                def ban_user(self, user_id: int) -> bool:
                    return True
        """)
        new_file = repo_on_disk / "admin.py"
        new_file.write_text(new_content)

        removed_ids, new_nodes, new_edges = await indexing_service.index_files(
            graph=original,
            repo_name="my_app",
            repo_path=repo_on_disk,
            changed_files={"admin.py"},
            deleted_files=set(),
            language_hint="python",
        )

        assert removed_ids == []
        assert any(n.name == "AdminService" for n in new_nodes)
        assert original.has_node("my_app:admin.py")
        assert original.node_count > original_node_count

    @pytest.mark.asyncio
    async def test_incremental_delete_removes_nodes(
        self,
        indexing_service: IndexingService,
        repo_on_disk: Path,
    ) -> None:
        original = await indexing_service.index_all()
        assert original.has_node("my_app:routes.py")

        # Delete routes.py from disk and run incremental
        (repo_on_disk / "routes.py").unlink()

        removed_ids, new_nodes, new_edges = await indexing_service.index_files(
            graph=original,
            repo_name="my_app",
            repo_path=repo_on_disk,
            changed_files=set(),
            deleted_files={"routes.py"},
        )

        assert "my_app:routes.py" in removed_ids
        assert not original.has_node("my_app:routes.py")


class TestGraphStats:
    """Verify graph statistics after indexing."""

    @pytest.mark.asyncio
    async def test_stats_are_populated(
        self, indexing_service: IndexingService,
    ) -> None:
        graph = await indexing_service.index_all()
        stats = graph.stats()

        assert stats["total_nodes"] > 0
        assert stats["total_edges"] > 0
        assert "my_app" in stats.get("repos", [])
