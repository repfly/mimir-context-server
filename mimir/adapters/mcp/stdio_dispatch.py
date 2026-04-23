"""Request dispatch for stdio MCP."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mimir.adapters.mcp.stdio_protocol import error_response, initialize_result, response, tool_definitions
from mimir.adapters.shared.session_context import apply_session_context
from mimir.domain.guardrails_config import load_agent_policy, load_rules
from mimir.services.agent_policy import AgentPolicy

logger = logging.getLogger(__name__)


async def handle_request(container, graph, workspace_name: str, request: dict) -> dict:
    method = request.get("method", "")
    params = request.get("params", {})
    request_id = request.get("id")

    try:
        if method == "initialize":
            return response(request_id, initialize_result(workspace_name))

        if method == "tools/list":
            return response(request_id, {"tools": tool_definitions()})

        if method == "notifications/initialized":
            return {}

        if method != "tools/call":
            return error_response(request_id, -32601, f"Unknown method: {method}")

        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        return await _handle_tool_call(container, graph, request_id, tool_name, tool_args, workspace_name)
    except Exception as exc:
        logger.error("MCP request failed: %s", exc, exc_info=True)
        return error_response(request_id, -32000, str(exc))


async def _handle_tool_call(container, graph, request_id, tool_name: str | None, tool_args: dict, workspace_name: str) -> dict:
    if tool_name == "get_context":
        bundle = await container.retrieval.search(
            query=tool_args["query"],
            graph=graph,
            token_budget=tool_args.get("budget"),
            repos=tool_args.get("repos"),
        )
        apply_session_context(
            container,
            bundle,
            query=tool_args["query"],
            session_id=tool_args.get("session_id"),
            budget=tool_args.get("budget"),
        )
        return response(request_id, {"content": [{"type": "text", "text": bundle.format_for_llm()}]})

    if tool_name == "get_graph_stats":
        stats = graph.stats()
        stats["workspace"] = workspace_name
        return response(request_id, {"content": [{"type": "text", "text": json.dumps(stats, indent=2)}]})

    if tool_name == "get_hotspots":
        results = container.temporal.get_hotspots(graph, top_n=tool_args.get("top_n", 20))
        payload = [{"node": node.id, "score": f"{score:.3f}", "changes": node.modification_count} for node, score in results]
        return response(request_id, {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]})

    if tool_name == "get_write_context":
        write_context = container.write_context.assemble(file_path=tool_args["file_path"], graph=graph)
        return response(request_id, {"content": [{"type": "text", "text": write_context.format_for_llm()}]})

    if tool_name == "get_impact":
        result = container.impact.analyze(
            graph,
            node_id=tool_args.get("node_id"),
            file_path=tool_args.get("file_path"),
            symbol_name=tool_args.get("symbol_name"),
            max_hops=tool_args.get("max_hops", 3),
        )
        text = "No matching symbol or file found for impact analysis." if result is None else result.format_for_llm()
        return response(request_id, {"content": [{"type": "text", "text": text}]})

    if tool_name == "get_quality":
        overview = container.quality.detect_gaps(
            graph,
            repos=tool_args.get("repos"),
            threshold=tool_args.get("threshold"),
            top_n=tool_args.get("top_n", 50),
        )
        return response(request_id, {"content": [{"type": "text", "text": overview.format_for_llm()}]})

    if tool_name == "get_catalog":
        catalog = container.catalog.generate_catalog(graph, repos=tool_args.get("repos"))
        return response(request_id, {"content": [{"type": "text", "text": catalog.format_for_llm()}]})

    if tool_name == "get_catalog_drift":
        report = container.catalog.detect_drift(
            graph,
            repo=tool_args["repo"],
            declared_deps=tool_args.get("declared_dependencies", []),
        )
        return response(request_id, {"content": [{"type": "text", "text": report.format_for_llm()}]})

    if tool_name == "validate_change":
        rules_path = Path(tool_args.get("rules_path", "mimir-rules.yaml"))
        rules = load_rules(rules_path)
        result = await container.guardrail.evaluate(graph, tool_args["diff"], rules)
        return response(request_id, {"content": [{"type": "text", "text": result.format_for_llm()}]})

    if tool_name == "can_i_modify":
        policy_path = Path(tool_args.get("policy_path", "mimir-agent-policy.yaml"))
        try:
            raw_policies = load_agent_policy(policy_path)
            policy = AgentPolicy.from_dict(raw_policies[0]) if raw_policies else None
        except Exception:
            policy = None

        file_path = tool_args["file_path"]
        if policy is None:
            text = f"File: {file_path}\nNo agent policy found — allowed by default."
        else:
            allowed = container.agent_policy.check_file_access(policy, file_path)
            text = (
                f"File: {file_path}\n"
                f"Policy: {policy.name}\n"
                f"Allowed: {'yes' if allowed else 'NO — outside agent scope'}"
            )
        return response(request_id, {"content": [{"type": "text", "text": text}]})

    if tool_name == "report_feedback":
        signal = container.feedback.record_explicit(
            node_ids=tool_args["node_ids"],
            outcome=tool_args["outcome"],
            session_id=tool_args.get("session_id"),
            query=tool_args.get("query"),
        )
        text = f"Feedback recorded: {signal.outcome} for {len(signal.node_ids)} nodes."
        return response(request_id, {"content": [{"type": "text", "text": text}]})

    return error_response(request_id, -32601, f"Unknown tool: {tool_name}")
