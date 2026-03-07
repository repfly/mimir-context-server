"""Web inspector server — aiohttp API for the frontend UI."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aiohttp import web

from mimir.container import Container
from mimir.domain.config import MimirConfig

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def run_web_server(config: MimirConfig, port: int = 8420) -> None:
    """Start the aiohttp web server."""
    container = Container(config)
    graph = container.load_graph()

    routes = web.RouteTableDef()

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    @routes.get("/api/stats")
    async def api_stats(request: web.Request) -> web.Response:
        return web.json_response(graph.stats())

    @routes.get("/api/nodes")
    async def api_nodes(request: web.Request) -> web.Response:
        kind = request.query.get("kind")
        repo = request.query.get("repo")
        limit = int(request.query.get("limit", "100"))
        offset = int(request.query.get("offset", "0"))

        nodes = list(graph.all_nodes())
        if kind:
            nodes = [n for n in nodes if n.kind.value == kind]
        if repo:
            nodes = [n for n in nodes if n.repo == repo]

        total = len(nodes)
        nodes = nodes[offset : offset + limit]

        return web.json_response({
            "total": total,
            "offset": offset,
            "limit": limit,
            "nodes": [n.to_dict() for n in nodes],
        })

    @routes.get("/api/node-detail")
    async def api_node_detail_query(request: web.Request) -> web.Response:
        """Node detail using query param to avoid URL encoding issues."""
        node_id = request.query.get("id", "")
        if not node_id:
            return web.json_response({"error": "Missing 'id' parameter"}, status=400)

        node = graph.get_node(node_id)
        if not node:
            raise web.HTTPNotFound(text=f"Node not found: {node_id}")

        outgoing = graph.get_outgoing_edges(node_id)
        incoming = graph.get_incoming_edges(node_id)
        children = graph.get_children(node_id)
        parent = graph.get_parent(node_id)

        node_data = node.to_dict()
        node_data["has_embedding"] = node.embedding is not None
        node_data["embedding_dim"] = len(node.embedding) if node.embedding else 0

        return web.json_response({
            "node": node_data,
            "outgoing_edges": [e.to_dict() for e in outgoing],
            "incoming_edges": [e.to_dict() for e in incoming],
            "children": [c.to_dict() for c in children],
            "parent": parent.to_dict() if parent else None,
        })

    @routes.get("/api/nodes/{node_id:.*}")
    async def api_node_detail(request: web.Request) -> web.Response:
        node_id = request.match_info["node_id"]
        node = graph.get_node(node_id)
        if not node:
            raise web.HTTPNotFound(text=f"Node not found: {node_id}")

        outgoing = graph.get_outgoing_edges(node_id)
        incoming = graph.get_incoming_edges(node_id)
        children = graph.get_children(node_id)
        parent = graph.get_parent(node_id)

        return web.json_response({
            "node": node.to_dict(),
            "outgoing_edges": [e.to_dict() for e in outgoing],
            "incoming_edges": [e.to_dict() for e in incoming],
            "children": [c.to_dict() for c in children],
            "parent": parent.to_dict() if parent else None,
        })

    @routes.get("/api/graph-data")
    async def api_graph_data(request: web.Request) -> web.Response:
        """Return D3-compatible graph data."""
        repo = request.query.get("repo")
        max_nodes = int(request.query.get("max", "200"))

        nodes = list(graph.all_nodes())
        if repo:
            nodes = [n for n in nodes if n.repo == repo]
        nodes = nodes[:max_nodes]

        node_ids = {n.id for n in nodes}
        edges = [
            e for e in graph.all_edges()
            if e.source in node_ids and e.target in node_ids
        ]

        return web.json_response({
            "nodes": [
                {"id": n.id, "name": n.name, "kind": n.kind.value, "repo": n.repo}
                for n in nodes
            ],
            "links": [
                {"source": e.source, "target": e.target, "kind": e.kind.value}
                for e in edges
            ],
        })

    @routes.get("/api/search")
    async def api_search(request: web.Request) -> web.Response:
        query = request.query.get("q", "")
        if not query:
            return web.json_response({"error": "Missing query parameter 'q'"}, status=400)

        budget = int(request.query.get("budget", "4000"))
        repo_filter = request.query.get("repo")
        repos = [repo_filter] if repo_filter else None

        bundle = await container.retrieval.search(
            query=query,
            graph=graph,
            token_budget=budget,
            repos=repos,
        )

        return web.json_response({
            "summary": bundle.summary,
            "token_count": bundle.token_count,
            "repos": bundle.repos_involved,
            "nodes": [n.to_dict() for n in bundle.nodes],
            "formatted": bundle.format_for_llm(),
        })

    @routes.get("/api/hotspots")
    async def api_hotspots(request: web.Request) -> web.Response:
        top_n = int(request.query.get("top", "20"))
        results = container.temporal.get_hotspots(graph, top_n=top_n)
        return web.json_response([
            {
                "node": n.to_dict(),
                "score": round(s, 4),
            }
            for n, s in results
        ])

    @routes.delete("/api/clear")
    async def api_clear(request: web.Request) -> web.Response:
        """Delete all locally stored index data.

        Accepts an optional JSON body to control what is cleared:
            {"graph": true, "sessions": true}

        Both default to true if omitted.
        """
        nonlocal graph
        try:
            body = await request.json()
        except Exception:
            body = {}

        clear_graph = body.get("graph", True)
        clear_sessions = body.get("sessions", True)

        result = container.clear_data(graph=clear_graph, sessions=clear_sessions)

        # Reload graph in-place so the server stays functional for new indexing
        if clear_graph:
            graph = container.load_graph()

        return web.json_response(result)

    # ------------------------------------------------------------------
    # Static files
    # ------------------------------------------------------------------

    @routes.get("/")
    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    routes.static("/static", _STATIC_DIR)

    # ------------------------------------------------------------------
    # CORS middleware
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
    # Assemble and run
    # ------------------------------------------------------------------

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_routes(routes)

    logger.info("Starting web inspector on port %d", port)
    web.run_app(app, host="0.0.0.0", port=port, print=lambda _: None, access_log=None)
