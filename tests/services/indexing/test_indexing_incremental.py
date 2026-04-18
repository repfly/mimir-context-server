from __future__ import annotations

from types import SimpleNamespace

import pytest

from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
from mimir.ports.parser import Symbol
from mimir.services.indexing import IndexingService


class _ParserStub:
    async def parse_file(self, file_path: str, language: str | None = None) -> list[Symbol]:
        return [
            Symbol(
                name="handle",
                kind="function",
                relative_path="src/features/service.py",
                start_line=1,
                end_line=2,
                code="def handle():\n    return 1\n",
            )
        ]

    def extract_identifiers(self, code: str, language: str | None = None, file_path: str | None = None) -> set[str]:
        return set()


@pytest.mark.asyncio
async def test_index_files_creates_missing_module_hierarchy(tmp_path) -> None:
    repo_path = tmp_path / "repo"
    file_path = repo_path / "src" / "features" / "service.py"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("def handle():\n    return 1\n")

    service = IndexingService.__new__(IndexingService)
    service._config = SimpleNamespace(
        indexing=SimpleNamespace(
            max_file_size_kb=500,
            excluded_patterns=[],
        ),
    )
    service._parser = _ParserStub()

    async def _no_embed(graph, mode, *, nodes_to_embed=None, show_progress=False) -> None:
        return None

    service._embed_and_upsert = _no_embed

    graph = CodeGraph()
    removed_ids, new_nodes, new_edges = await service.index_files(
        graph=graph,
        repo_name="repo",
        repo_path=repo_path,
        changed_files={"src/features/service.py"},
        deleted_files=set(),
        language_hint="python",
    )

    assert removed_ids == []
    assert graph.has_node("repo:")
    assert graph.has_node("repo:src/")
    assert graph.has_node("repo:src/features/")
    assert graph.has_node("repo:src/features/service.py")
    assert any(node.id == "repo:src/" for node in new_nodes)
    assert any(node.id == "repo:src/features/" for node in new_nodes)
    assert any(edge.source == "repo:" and edge.target == "repo:src/" for edge in new_edges)
    assert any(edge.source == "repo:src/" and edge.target == "repo:src/features/" for edge in new_edges)
