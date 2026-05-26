from __future__ import annotations

from types import SimpleNamespace

import pytest

from mimir.adapters.mcp.stdio_dispatch import handle_request
from mimir.adapters.mcp.stdio_protocol import tool_definitions
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
from mimir.services.impact import ImpactService
from mimir.services.write_context import WriteContextService


def _runtime() -> SimpleNamespace:
    graph = CodeGraph()
    file_node = Node(
        id="repo:src/service.py",
        repo="repo",
        kind=NodeKind.FILE,
        name="service.py",
        path="src/service.py",
    )
    func_node = Node(
        id="repo:src/service.py::handle",
        repo="repo",
        kind=NodeKind.FUNCTION,
        name="handle",
        path="src/service.py",
        raw_code="def handle():\n    return 1\n",
    )
    graph.add_node(file_node)
    graph.add_node(func_node)
    return SimpleNamespace(
        graph=graph,
        require_graph=lambda: graph,
        write_context=WriteContextService(),
        impact=ImpactService(),
    )


def test_stdio_tool_definitions_include_core_agent_tools() -> None:
    names = {tool["name"] for tool in tool_definitions()}

    assert "get_write_context" in names
    assert "get_impact" in names
    assert "validate_change" in names
    assert "can_i_modify" in names


@pytest.mark.asyncio
async def test_stdio_get_write_context_uses_query_runtime_graph() -> None:
    result = await handle_request(
        _runtime(),
        "default",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_write_context",
                "arguments": {"file_path": "service.py"},
            },
        },
    )

    text = result["result"]["content"][0]["text"]
    assert "Write context" in text
    assert "service.py" in text


@pytest.mark.asyncio
async def test_stdio_get_impact_uses_query_runtime_graph() -> None:
    result = await handle_request(
        _runtime(),
        "default",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_impact",
                "arguments": {"file_path": "service.py"},
            },
        },
    )

    text = result["result"]["content"][0]["text"]
    assert "Impact analysis" in text
    assert "service.py" in text
