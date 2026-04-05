"""HTTP API server — shared remote Mimir server for team access.

Exposes the same query tools as the MCP server over HTTP JSON endpoints,
so developers who don't have repos cloned locally can query a centrally
indexed codebase.

Endpoints
---------
POST /api/v1/context        — get_context (primary search tool)
GET  /api/v1/stats          — get_graph_stats
GET  /api/v1/hotspots       — get_hotspots
GET  /api/v1/quality        — get_quality
GET  /api/v1/catalog        — Backstage-compatible service catalog
GET  /api/v1/catalog/{repo} — single-service catalog entry
POST /api/v1/catalog/drift  — dependency drift detection
POST /api/v1/clear          — clear_data (admin only, not exposed via MCP)
GET  /api/v1/health         — health check

POST /api/v1/mcp         — raw MCP JSON-RPC (for MCP-over-HTTP proxy clients)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from mimir.container import Container
from mimir.domain.config import MimirConfig

logger = logging.getLogger(__name__)


def run_http_server(
    config: MimirConfig,
    host: str = "0.0.0.0",
    port: int = 8421,
    workspace_name: str | None = None,
) -> None:
    """Start the shared HTTP API server.

    This is the server that the *team* runs centrally.  Mobile developers
    (or any consumer who doesn't have the repos locally) connect to it
    via ``mimir serve --remote http://<host>:<port>``.
    """
    container = Container(config)
    graph = container.load_graph()
    _ws_label = workspace_name or "default"

    logger.info(
        "HTTP server starting — workspace=%s, host=%s, port=%d, graph=%d nodes",
        _ws_label, host, port, graph.node_count,
    )

    routes = web.RouteTableDef()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @routes.get("/api/v1/health")
    async def health(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "workspace": _ws_label,
            "graph_nodes": graph.node_count,
            "graph_edges": graph.edge_count,
        })

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    @routes.post("/api/v1/context")
    async def api_context(request: web.Request) -> web.Response:
        """Search the code graph and return a context bundle."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        query = body.get("query")
        if not query:
            return web.json_response({"error": "Missing 'query' field"}, status=400)

        budget = body.get("budget")
        repos = body.get("repos")
        session_id = body.get("session_id")

        try:
            bundle = await container.retrieval.search(
                query=query,
                graph=graph,
                token_budget=budget,
                repos=repos,
            )

            # Session handling
            if session_id:
                session = container.session.get_or_create(session_id)
                sg = _bundle_to_subgraph(bundle)
                container.session.session_dedup(
                    sg, session, query_embedding=bundle.query_embedding,
                )
                bundle.nodes = list(sg.nodes.values())
                bundle.edges = sg.edges
                bundle.token_count = sg.token_estimate
                if sg.notes:
                    bundle.session_note = (
                        "Previously seen chunks omitted: " + str(len(sg.notes))
                    )
                container.session.record_retrieval(
                    session,
                    query,
                    bundle.nodes,
                    {n.id: 1.0 for n in bundle.nodes},
                    query_embedding=bundle.query_embedding,
                )

            return web.json_response({
                "summary": bundle.summary,
                "token_count": bundle.token_count,
                "repos": bundle.repos_involved,
                "session_note": bundle.session_note,
                "formatted": bundle.format_for_llm(),
                "nodes": [n.to_dict() for n in bundle.nodes],
            })
        except Exception as exc:
            logger.error("Context query failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.get("/api/v1/stats")
    async def api_stats(request: web.Request) -> web.Response:
        stats = graph.stats()
        stats["workspace"] = _ws_label
        return web.json_response(stats)

    @routes.get("/api/v1/hotspots")
    async def api_hotspots(request: web.Request) -> web.Response:
        top_n = int(request.query.get("top", "20"))
        results = container.temporal.get_hotspots(graph, top_n=top_n)
        return web.json_response([
            {"node": n.id, "score": round(s, 4), "changes": n.modification_count}
            for n, s in results
        ])

    @routes.get("/api/v1/quality")
    async def api_quality(request: web.Request) -> web.Response:
        """Analyze graph quality and detect gaps."""
        repos_param = request.query.get("repos")
        repos = repos_param.split(",") if repos_param else None
        threshold = float(request.query.get("threshold", "0.3"))
        top_n = int(request.query.get("top_n", "50"))
        overview = container.quality.detect_gaps(
            graph, repos=repos, threshold=threshold, top_n=top_n,
        )
        return web.json_response(overview.to_dict())

    # ------------------------------------------------------------------
    # Catalog API (Backstage integration)
    # ------------------------------------------------------------------

    @routes.get("/api/v1/catalog")
    async def api_catalog(request: web.Request) -> web.Response:
        """Generate Backstage-compatible service catalog from the code graph."""
        try:
            repos_param = request.query.get("repos")
            repos = repos_param.split(",") if repos_param else None
            response = container.catalog.generate_catalog(graph, repos=repos)
            return web.json_response(response.to_dict())
        except Exception as exc:
            logger.error("Catalog generation failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.get("/api/v1/catalog/{repo}")
    async def api_catalog_service(request: web.Request) -> web.Response:
        """Get catalog entry for a single service/repo."""
        repo = request.match_info["repo"]
        try:
            response = container.catalog.generate_catalog(graph, repos=[repo])
            if not response.services:
                return web.json_response(
                    {"error": f"Repo '{repo}' not found in graph"}, status=404,
                )
            return web.json_response(response.services[0].to_dict())
        except Exception as exc:
            logger.error("Catalog single-service failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/api/v1/catalog/drift")
    async def api_catalog_drift(request: web.Request) -> web.Response:
        """Compare declared dependencies against code-analyzed reality."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        repo = body.get("repo")
        if not repo:
            return web.json_response({"error": "Missing 'repo' field"}, status=400)

        declared_deps = body.get("declared_dependencies", [])
        try:
            report = container.catalog.detect_drift(
                graph, repo, declared_deps,
            )
            return web.json_response(report.to_dict())
        except Exception as exc:
            logger.error("Drift detection failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------

    @routes.post("/api/v1/clear")
    async def api_clear(request: web.Request) -> web.Response:
        nonlocal graph
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = container.clear_data(
            graph=body.get("graph", True),
            sessions=body.get("sessions", True),
        )
        if body.get("graph", True):
            graph = container.load_graph()
        return web.json_response(result)

    # ------------------------------------------------------------------
    # MCP-over-HTTP  (JSON-RPC passthrough)
    # ------------------------------------------------------------------

    @routes.post("/api/v1/mcp")
    async def mcp_passthrough(request: web.Request) -> web.Response:
        """Accept a raw MCP JSON-RPC request and return the response.

        This is how ``mimir serve --remote`` connects: it wraps each
        stdio JSON-RPC message in an HTTP POST to this endpoint.
        """
        try:
            rpc_request = await request.json()
        except Exception:
            return web.json_response(
                _rpc_error(None, -32700, "Parse error"), status=400,
            )

        method = rpc_request.get("method", "")
        params = rpc_request.get("params", {})
        request_id = rpc_request.get("id")

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "mimir",
                        "version": "1.0.0",
                        "workspace": _ws_label,
                        "transport": "http",
                    },
                }
                return web.json_response(_rpc_ok(request_id, result))

            elif method == "tools/list":
                tools = _tool_definitions()
                return web.json_response(_rpc_ok(request_id, {"tools": tools}))

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
                    session_id = tool_args.get("session_id")
                    if session_id:
                        session = container.session.get_or_create(session_id)
                        sg = _bundle_to_subgraph(bundle)
                        container.session.session_dedup(
                            sg, session, query_embedding=bundle.query_embedding,
                        )
                        bundle.nodes = list(sg.nodes.values())
                        bundle.edges = sg.edges
                        bundle.token_count = sg.token_estimate
                        if sg.notes:
                            bundle.session_note = (
                                "Previously seen chunks omitted: "
                                + str(len(sg.notes))
                            )
                        container.session.record_retrieval(
                            session,
                            tool_args["query"],
                            bundle.nodes,
                            {n.id: 1.0 for n in bundle.nodes},
                            query_embedding=bundle.query_embedding,
                        )
                    return web.json_response(_rpc_ok(request_id, {
                        "content": [{
                            "type": "text",
                            "text": bundle.format_for_llm(),
                        }],
                    }))

                elif tool_name == "get_graph_stats":
                    stats = graph.stats()
                    stats["workspace"] = _ws_label
                    return web.json_response(_rpc_ok(request_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(stats, indent=2),
                        }],
                    }))

                elif tool_name == "get_hotspots":
                    top_n = tool_args.get("top_n", 20)
                    results = container.temporal.get_hotspots(graph, top_n=top_n)
                    hotspots = [
                        {"node": n.id, "score": f"{s:.3f}", "changes": n.modification_count}
                        for n, s in results
                    ]
                    return web.json_response(_rpc_ok(request_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(hotspots, indent=2),
                        }],
                    }))

                elif tool_name == "get_quality":
                    overview = container.quality.detect_gaps(
                        graph,
                        repos=tool_args.get("repos"),
                        threshold=tool_args.get("threshold"),
                        top_n=tool_args.get("top_n", 50),
                    )
                    return web.json_response(_rpc_ok(request_id, {
                        "content": [{
                            "type": "text",
                            "text": overview.format_for_llm(),
                        }],
                    }))

                elif tool_name == "get_catalog":
                    cat_response = container.catalog.generate_catalog(
                        graph, repos=tool_args.get("repos"),
                    )
                    return web.json_response(_rpc_ok(request_id, {
                        "content": [{
                            "type": "text",
                            "text": cat_response.format_for_llm(),
                        }],
                    }))

                elif tool_name == "get_catalog_drift":
                    drift_report = container.catalog.detect_drift(
                        graph,
                        repo=tool_args["repo"],
                        declared_deps=tool_args.get("declared_dependencies", []),
                    )
                    return web.json_response(_rpc_ok(request_id, {
                        "content": [{
                            "type": "text",
                            "text": drift_report.format_for_llm(),
                        }],
                    }))

                else:
                    return web.json_response(
                        _rpc_error(request_id, -32601, f"Unknown tool: {tool_name}"),
                    )

            elif method == "notifications/initialized":
                return web.json_response({})

            else:
                return web.json_response(
                    _rpc_error(request_id, -32601, f"Unknown method: {method}"),
                )

        except Exception as exc:
            logger.error("MCP-over-HTTP error: %s", exc, exc_info=True)
            return web.json_response(
                _rpc_error(request_id, -32000, str(exc)),
            )

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            response = web.Response(status=200)
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_routes(routes)

    # Eagerly load embedding model so first query is instant
    container.warmup()

    logger.info("Shared Mimir HTTP server listening on http://%s:%d", host, port)
    web.run_app(app, host=host, port=port, print=lambda _: None, access_log=None)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _rpc_ok(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _bundle_to_subgraph(bundle):
    from mimir.domain.subgraph import SubGraph
    sg = SubGraph()
    for n in bundle.nodes:
        sg.add_node(n)
    for e in bundle.edges:
        sg.add_edge(e)
    return sg


def _tool_definitions() -> list[dict]:
    """Return the MCP tools list (same as stdio MCP server)."""
    return [
        {
            "name": "get_context",
            "description": (
                "Retrieve relevant source code context for a natural language query. "
                "Call this BEFORE answering any question about how the codebase works, "
                "what a function does, where a feature is implemented, or how components interact. "
                "Returns a minimal, connected, token-budget-aware context bundle assembled from "
                "the code graph. "
                "Use `session_id` to enable cross-turn deduplication. "
                "Use `repos` to restrict results to specific repositories. "
                "Use `budget` to control the maximum token count (default 8000)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language question or task description",
                    },
                    "budget": {
                        "type": "integer",
                        "description": "Maximum tokens in the context bundle. Default: 8000.",
                    },
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo names to restrict search to.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Conversation session ID for deduplication.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_graph_stats",
            "description": (
                "Return statistics about the indexed code graph: node count, edge count, "
                "breakdown by kind, and repos indexed."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_hotspots",
            "description": (
                "Return the most recently and frequently modified code nodes."
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
            "name": "get_quality",
            "description": (
                "Analyze graph connectivity quality and detect gaps — nodes with missing connections."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo names to restrict analysis to.",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Quality threshold for gap detection. Default: 0.3.",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Max gap nodes to return. Default: 50.",
                    },
                },
            },
        },
        {
            "name": "get_catalog",
            "description": (
                "Generate a Backstage-compatible service catalog from the code graph. "
                "Returns services with APIs, dependencies, tech stack, ownership, and quality."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo names to include. Omit for all repos.",
                    },
                },
            },
        },
        {
            "name": "get_catalog_drift",
            "description": (
                "Compare declared dependencies against code-analyzed reality. "
                "Returns drift score and categorized findings."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name to check.",
                    },
                    "declared_dependencies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                            },
                            "required": ["name"],
                        },
                        "description": "Declared dependencies to compare against.",
                    },
                },
                "required": ["repo", "declared_dependencies"],
            },
        },
    ]
