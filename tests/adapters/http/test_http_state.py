from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mimir.adapters.http.state import HttpServerState
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind


def _graph_with_node(node_id: str) -> CodeGraph:
    graph = CodeGraph()
    graph.add_node(Node(
        id=node_id,
        repo="repo",
        kind=NodeKind.REPOSITORY,
        name=node_id,
    ))
    return graph


@pytest.mark.asyncio
async def test_refresh_repo_uses_detached_graph_and_swaps_on_success() -> None:
    live_graph = _graph_with_node("live:")
    working_graph = _graph_with_node("working:")
    invalidations: list[str] = []
    replaced: list[CodeGraph] = []

    async def refresh_repo(graph: CodeGraph, repo_name: str) -> dict:
        await asyncio.sleep(0)
        graph.add_node(Node(
            id=f"{repo_name}:new",
            repo=repo_name,
            kind=NodeKind.FILE,
            name="new.py",
            path="new.py",
        ))
        return {"repo": repo_name}

    state = HttpServerState(
        container=SimpleNamespace(
            graph_store=SimpleNamespace(load=lambda: working_graph),
            indexing=SimpleNamespace(refresh_repo=refresh_repo),
            retrieval=SimpleNamespace(invalidate_bm25=lambda: invalidations.append("bm25")),
            replace_graph=lambda graph: replaced.append(graph),
            load_graph=lambda force_reload=False: live_graph,
            clear_data=lambda **kwargs: {"cleared": []},
        ),
        workspace_name="default",
        graph=live_graph,
    )

    result = await state.refresh_repo("repo")

    assert result == {"repo": "repo"}
    assert state.graph is working_graph
    assert live_graph.get_node("repo:new") is None
    assert working_graph.get_node("repo:new") is not None
    assert replaced == [working_graph]
    assert invalidations == ["bm25"]
