"""MCP server adapter — stdio transport for IDE integration."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from mimir.container import Container
from mimir.domain.config import MimirConfig

logger = logging.getLogger(__name__)


def run_mcp_server(config: MimirConfig, workspace_name: str | None = None) -> None:
    """Start the MCP server on stdio.

    Parameters
    ----------
    config:
        Validated Mimir configuration.
    workspace_name:
        The workspace name this server is locked to (for informational purposes only).
        If None, the server is running against a bare --config file.
    """
    _ws_label = workspace_name or "default"
    container = Container(config)
    graph = container.load_graph()
    container.warmup()
    logger.info(
        "MCP server starting — workspace=%s, graph has %d nodes",
        _ws_label, graph.node_count,
    )

    async def handle_request(request: dict) -> dict:
        """Route MCP JSON-RPC requests."""
        nonlocal graph  # clear_data may reload the graph; declare nonlocal to avoid UnboundLocalError
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        try:
            if method == "initialize":
                return _response(request_id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "mimir",
                        "version": "1.0.0",
                        "workspace": _ws_label,
                    },
                })

            elif method == "tools/list":
                return _response(request_id, {
                    "tools": [
                        {
                            "name": "get_context",
                            "description": (
                                "Retrieve relevant source code context for a natural language query. "
                                "Call this BEFORE answering any question about how the codebase works, "
                                "what a function does, where a feature is implemented, or how components interact. "
                                "Returns a minimal, connected, token-budget-aware context bundle assembled from "
                                "the code graph — including the most relevant functions, classes, and their "
                                "dependencies. "
                                "Use `session_id` to enable cross-turn deduplication: pass the same ID on every "
                                "turn of a conversation so that code already seen by the LLM is summarized or "
                                "omitted in subsequent responses, reducing token usage. "
                                "Use `repos` to restrict results to specific repositories when working in a "
                                "multi-repo workspace. "
                                "Use `budget` to control the maximum token count of the returned context "
                                "(default 8000)."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Natural language question or task description, e.g. 'how does authentication work' or 'where is the retry logic for API calls'",
                                    },
                                    "budget": {
                                        "type": "integer",
                                        "description": "Maximum tokens to include in the context bundle. Default: 8000. Reduce for faster responses or when only a summary is needed.",
                                    },
                                    "repos": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Optional list of repo names to restrict the search to. Omit to search all indexed repos.",
                                    },
                                    "session_id": {
                                        "type": "string",
                                        "description": "Conversation session ID. Pass the same value on every turn to enable deduplication — code sent in a previous turn will be summarized or omitted, reducing repeated context.",
                                    },
                                },
                                "required": ["query"],
                            },
                        },
                        {
                            "name": "get_graph_stats",
                            "description": (
                                "Return statistics about the indexed code graph: total node count, edge count, "
                                "breakdown by node kind (functions, classes, files, modules), and repos indexed. "
                                "Call this when the user asks what has been indexed, how large the codebase is, "
                                "or to confirm that indexing completed successfully before proceeding with queries."
                            ),
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                        {
                            "name": "get_hotspots",
                            "description": (
                                "Return the most recently and frequently modified code nodes, ranked by a "
                                "combined recency + change-frequency score. "
                                "Call this when the user asks about active development areas, what has changed "
                                "recently, where bugs are most likely introduced, or what to focus a code review on. "
                                "Results include node ID, hotspot score, and commit count."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "top_n": {
                                        "type": "integer",
                                        "description": "Number of hotspots to return. Default: 20.",
                                    },
                                },
                            },
                        },
                        {
                            "name": "clear_data",
                            "description": (
                                "Delete locally stored index data. "
                                "Call this when the user explicitly asks to clear, reset, or wipe the index, "
                                "or when the codebase has changed significantly and needs to be re-indexed from scratch. "
                                "After clearing the graph, the user must run `mimir index` before queries will work again. "
                                "Clearing sessions only removes conversation history and does not affect the code index."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "graph": {
                                        "type": "boolean",
                                        "description": "Clear the code graph and all embeddings. Default: true.",
                                    },
                                    "sessions": {
                                        "type": "boolean",
                                        "description": "Clear all conversation sessions. Default: true.",
                                    },
                                },
                            },
                        },
                    ],
                })

            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})

                if tool_name == "get_context":
                    bundle = await container.retrieval.search(
                        query=tool_args["query"],
                        graph=graph,
                        token_budget=tool_args.get("budget"),
                        repos=tool_args.get("repos"),
                    )

                    # Session handling
                    session_id = tool_args.get("session_id")
                    if session_id:
                        session = container.session.get_or_create(session_id)
                        sg = _bundle_to_subgraph(bundle)
                        container.session.session_dedup(sg, session)
                        
                        # Apply deduplication back to the bundle
                        bundle.nodes = list(sg.nodes.values())
                        bundle.edges = sg.edges
                        bundle.token_count = sg.token_estimate
                        if sg.notes:
                            bundle.session_note = "Previously seen chunks omitted: " + str(len(sg.notes))
                            
                        container.session.record_retrieval(
                            session,
                            tool_args["query"],
                            bundle.nodes,
                            {n.id: 1.0 for n in bundle.nodes},
                        )

                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": bundle.format_for_llm(),
                        }],
                    })

                elif tool_name == "get_graph_stats":
                    stats = graph.stats()
                    stats["workspace"] = _ws_label
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(stats, indent=2),
                        }],
                    })

                elif tool_name == "get_hotspots":
                    top_n = tool_args.get("top_n", 20)
                    results = container.temporal.get_hotspots(graph, top_n=top_n)
                    hotspots = [
                        {"node": n.id, "score": f"{s:.3f}", "changes": n.modification_count}
                        for n, s in results
                    ]
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(hotspots, indent=2),
                        }],
                    })

                elif tool_name == "clear_data":
                    clear_graph = tool_args.get("graph", True)
                    clear_sessions = tool_args.get("sessions", True)
                    result = container.clear_data(graph=clear_graph, sessions=clear_sessions)
                    # Reload graph after clearing so subsequent calls work correctly
                    if clear_graph:
                        graph = container.load_graph()
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(result),
                        }],
                    })

                else:
                    return _error_response(request_id, -32601, f"Unknown tool: {tool_name}")

            elif method == "notifications/initialized":
                return {}  # Acknowledgement, no response needed

            else:
                return _error_response(request_id, -32601, f"Unknown method: {method}")

        except Exception as exc:
            logger.error("MCP request failed: %s", exc, exc_info=True)
            return _error_response(request_id, -32000, str(exc))

    async def main_loop():
        """Read JSON-RPC messages from stdin, write responses to stdout."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        loop = asyncio.get_event_loop()
        write_transport, _ = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )

        buffer = b""
        while True:
            try:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data

                # Try to parse JSON-RPC messages
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                        response = await handle_request(request)
                        if response and response.get("id") is not None:
                            out = json.dumps(response) + "\n"
                            write_transport.write(out.encode())
                    except json.JSONDecodeError:
                        continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("MCP loop error: %s", exc, exc_info=True)

    try:
        asyncio.run(main_loop())
    finally:
        container.close()


def _response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _bundle_to_subgraph(bundle):
    """Convert a ContextBundle back to a SubGraph for session dedup."""
    from mimir.domain.subgraph import SubGraph
    sg = SubGraph()
    for n in bundle.nodes:
        sg.add_node(n)
    for e in bundle.edges:
        sg.add_edge(e)
    return sg
