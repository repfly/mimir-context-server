"""MCP-over-HTTP routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aiohttp import web

from mimir.adapters.http.state import HttpServerState
from mimir.adapters.http.tooling import rpc_error, rpc_ok, tool_definitions
from mimir.adapters.shared.session_context import apply_session_context
from mimir.domain.errors import NoActiveIndexError
from mimir.domain.guardrails_config import load_agent_policy, load_rules
from mimir.services.agent_policy import AgentPolicy

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
    try:
        graph = state.runtime.require_graph()
    except NoActiveIndexError:
        return web.json_response(
            rpc_error(request_id, -32000, "No active index. Run: mimir indexer run"),
            status=503,
        )
    if tool_name == "get_context":
        bundle = await state.runtime.retrieval.search(
            query=tool_args["query"],
            graph=graph,
            token_budget=tool_args.get("budget"),
            repos=tool_args.get("repos"),
        )
        apply_session_context(
            state.runtime,
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
        results = state.runtime.temporal.get_hotspots(graph, top_n=tool_args.get("top_n", 20))
        hotspots = [{"node": node.id, "score": f"{score:.3f}", "changes": node.modification_count} for node, score in results]
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": json.dumps(hotspots, indent=2)}]}))

    if tool_name == "get_write_context":
        write_context = state.runtime.write_context.assemble(file_path=tool_args["file_path"], graph=graph)
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": write_context.format_for_llm()}]}))

    if tool_name == "get_impact":
        result = state.runtime.impact.analyze(
            graph,
            node_id=tool_args.get("node_id"),
            file_path=tool_args.get("file_path"),
            symbol_name=tool_args.get("symbol_name"),
            max_hops=tool_args.get("max_hops", 3),
        )
        text = "No matching symbol or file found for impact analysis." if result is None else result.format_for_llm()
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": text}]}))

    if tool_name == "get_quality":
        overview = state.runtime.quality.detect_gaps(
            graph,
            repos=tool_args.get("repos"),
            threshold=tool_args.get("threshold"),
            top_n=tool_args.get("top_n", 50),
        )
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": overview.format_for_llm()}]}))

    if tool_name == "get_catalog":
        response = state.runtime.catalog.generate_catalog(graph, repos=tool_args.get("repos"))
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": response.format_for_llm()}]}))

    if tool_name == "get_catalog_drift":
        report = state.runtime.catalog.detect_drift(
            graph,
            repo=tool_args["repo"],
            declared_deps=tool_args.get("declared_dependencies", []),
        )
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": report.format_for_llm()}]}))

    if tool_name == "validate_change":
        rules = load_rules(Path(tool_args.get("rules_path", "mimir-rules.yaml")))
        result = await state.runtime.guardrail.evaluate(graph, tool_args["diff"], rules)
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": result.format_for_llm()}]}))

    if tool_name == "can_i_modify":
        policy_path = Path(tool_args.get("policy_path", "mimir-agent-policy.yaml"))
        try:
            raw_policies = load_agent_policy(policy_path)
            policy = AgentPolicy.from_dict(raw_policies[0]) if raw_policies else None
        except Exception:
            policy = None
        file_path = tool_args["file_path"]
        if policy is None:
            text = f"File: {file_path}\nNo agent policy found - allowed by default."
        else:
            allowed = state.runtime.agent_policy.check_file_access(policy, file_path)
            text = (
                f"File: {file_path}\n"
                f"Policy: {policy.name}\n"
                f"Allowed: {'yes' if allowed else 'NO - outside agent scope'}"
            )
        return web.json_response(rpc_ok(request_id, {"content": [{"type": "text", "text": text}]}))

    return web.json_response(rpc_error(request_id, -32601, f"Unknown tool: {tool_name}"))
