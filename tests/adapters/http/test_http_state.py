from __future__ import annotations

from types import SimpleNamespace

from mimir.adapters.http.state import HttpServerState
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind


def test_http_state_reads_graph_from_query_runtime() -> None:
    graph = CodeGraph()
    graph.add_node(Node(
        id="repo:",
        repo="repo",
        kind=NodeKind.REPOSITORY,
        name="repo",
    ))
    state = HttpServerState(
        runtime=SimpleNamespace(graph=graph),
        workspace_name="default",
    )

    assert state.current_graph() is graph
