"""MCP-over-HTTP routes."""

from __future__ import annotations

import json
import logging

from aiohttp import web

from mimir.adapters.http.state import HttpServerState
from mimir.adapters.http.tooling import rpc_error, rpc_ok, tool_definitions
from mimir.adapters.shared.session_context import apply_session_context

logger = logging.getLogger(__name__)


def register_mcp_routes(routes: web.RouteTableDef, state: HttpServerState) -> None:
    @routes.post("/api/v1/mcp")
    async def mcp_passthrough(request: web.Request) -> web.Response:
        try:
            rpc_request = await request.json()
        except Exception:
            return web.json_response(rpc_error(None, -32700, "Parse error"), status=400)

        method = rpc_request.get("method", "")
        params = rpc_request.get("params", {})
        request_id = rpc_request.get("id")

        try:
            if method == "initialize":
                return web.json_response(rpc_ok(request_id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "mimir",
                        "version": "1.0.0",
                        "workspace": state.workspace_name,
                        "transport": "http",
                    },
                }))
            if method == "tools/list":
                return web.json_response(rpc_ok(request_id, {"tools": tool_definitions()}))
            if method == "notifications/initialized":
                return web.json_response({})
            if method != "tools/call":
                return web.json_response(rpc_error(request_id, -32601, f"Unknown method: {method}"))

            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            return await _handle_tool_call(state, request_id, tool_name, tool_args)
        except Exception as exc:
            logger.error("MCP-over-HTTP error: %s", exc, exc_info=True)
            return web.json_response(rpc_error(request_id, -32000, str(exc)))


async def _handle_tool_call(
    state: HttpServerState,
    request_id,
    tool_name: str | None,
    tool_args: dict,
) -> web.Response:
    graph = state.current_graph()
    if tool_name == "get_context":
        bundle = await state.container.retrieval.search(
            query=tool_args["query"],
            graph=graph,
            token_budget=tool_args.get("budget"),
            repos=tool_args.get("repos"),
        )
        apply_session_context(
            state.container,
            bundle,
            query=tool_args["query"],
            session_id=tool_args.get("session_id"),
            budget=tool_args.get("budget"),
        )
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": bundle.format_for_llm()}]}))

    if tool_name == "get_graph_stats":
        stats = graph.stats()
        stats["workspace"] = state.workspace_name
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": json.dumps(stats, indent=2)}]}))

    if tool_name == "get_hotspots":
        results = state.container.temporal.get_hotspots(graph, top_n=tool_args.get("top_n", 20))
        hotspots = [{"node": node.id, "score": f"{score:.3f}", "changes": node.modification_count} for node, score in results]
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": json.dumps(hotspots, indent=2)}]}))

    if tool_name == "get_quality":
        overview = state.container.quality.detect_gaps(
            graph,
            repos=tool_args.get("repos"),
            threshold=tool_args.get("threshold"),
            top_n=tool_args.get("top_n", 50),
        )
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": overview.format_for_llm()}]}))

    if tool_name == "get_catalog":
        response = state.container.catalog.generate_catalog(graph, repos=tool_args.get("repos"))
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": response.format_for_llm()}]}))

    if tool_name == "get_catalog_drift":
        report = state.container.catalog.detect_drift(
            graph,
            repo=tool_args["repo"],
            declared_deps=tool_args.get("declared_dependencies", []),
        )
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": report.format_for_llm()}]}))

    return web.json_response(rpc_error(request_id, -32601, f"Unknown tool: {tool_name}"))
