"""Route registration for the web inspector adapter."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from mimir.adapters.web.state import WebServerState

_STATIC_DIR = Path(__file__).parent / "static"


def register_routes(routes: web.RouteTableDef, state: WebServerState) -> None:
    @routes.get("/api/stats")
    async def api_stats(request: web.Request) -> web.Response:
        return web.json_response(state.current_graph().stats())

    @routes.get("/api/nodes")
    async def api_nodes(request: web.Request) -> web.Response:
        graph = state.current_graph()
        kind = request.query.get("kind")
        repo = request.query.get("repo")
        limit = int(request.query.get("limit", "100"))
        offset = int(request.query.get("offset", "0"))

        nodes = list(graph.all_nodes())
        if kind:
            nodes = [node for node in nodes if node.kind.value == kind]
        if repo:
            nodes = [node for node in nodes if node.repo == repo]

        total = len(nodes)
        nodes = nodes[offset : offset + limit]
        return web.json_response({
            "total": total,
            "offset": offset,
            "limit": limit,
            "nodes": [node.to_dict() for node in nodes],
        })

    @routes.get("/api/node-detail")
    async def api_node_detail_query(request: web.Request) -> web.Response:
        node_id = request.query.get("id", "")
        if not node_id:
            return web.json_response({"error": "Missing 'id' parameter"}, status=400)
        return _node_detail_response(state.current_graph(), node_id, include_embedding=True)

    @routes.get("/api/nodes/{node_id:.*}")
    async def api_node_detail(request: web.Request) -> web.Response:
        return _node_detail_response(state.current_graph(), request.match_info["node_id"])

    @routes.get("/api/graph-data")
    async def api_graph_data(request: web.Request) -> web.Response:
        graph = state.current_graph()
        repo = request.query.get("repo")
        max_nodes = int(request.query.get("max", "200"))

        nodes = list(graph.all_nodes())
        if repo:
            nodes = [node for node in nodes if node.repo == repo]
        nodes = nodes[:max_nodes]
        node_ids = {node.id for node in nodes}
        edges = [
            edge
            for edge in graph.all_edges()
            if edge.source in node_ids and edge.target in node_ids
        ]

        return web.json_response({
            "nodes": [{"id": node.id, "name": node.name, "kind": node.kind.value, "repo": node.repo} for node in nodes],
            "links": [{"source": edge.source, "target": edge.target, "kind": edge.kind.value} for edge in edges],
        })

    @routes.get("/api/search")
    async def api_search(request: web.Request) -> web.Response:
        query = request.query.get("q", "")
        if not query:
            return web.json_response({"error": "Missing query parameter 'q'"}, status=400)

        graph = state.current_graph()
        budget = int(request.query.get("budget", "4000"))
        repo_filter = request.query.get("repo")
        repos = [repo_filter] if repo_filter else None
        bundle = await state.container.retrieval.search(
            query=query,
            graph=graph,
            token_budget=budget,
            repos=repos,
        )
        return web.json_response({
            "summary": bundle.summary,
            "token_count": bundle.token_count,
            "repos": bundle.repos_involved,
            "nodes": [node.to_dict() for node in bundle.nodes],
            "formatted": bundle.format_for_llm(),
        })

    @routes.get("/api/hotspots")
    async def api_hotspots(request: web.Request) -> web.Response:
        graph = state.current_graph()
        top_n = int(request.query.get("top", "20"))
        results = state.container.temporal.get_hotspots(graph, top_n=top_n)
        return web.json_response([{"node": node.to_dict(), "score": round(score, 4)} for node, score in results])

    @routes.get("/api/quality")
    async def api_quality(request: web.Request) -> web.Response:
        graph = state.current_graph()
        repos_param = request.query.get("repos")
        overview = state.container.quality.detect_gaps(
            graph,
            repos=repos_param.split(",") if repos_param else None,
            threshold=float(request.query.get("threshold", "0.3")),
            top_n=int(request.query.get("top_n", "50")),
        )
        return web.json_response(overview.to_dict())

    @routes.delete("/api/clear")
    async def api_clear(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}

        result = state.container.clear_data(
            graph=body.get("graph", True),
            sessions=body.get("sessions", True),
        )
        if body.get("graph", True):
            state.reload_graph()
        return web.json_response(result)

    @routes.get("/")
    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    routes.static("/static", _STATIC_DIR, append_version=True)


def cors_middleware():
    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            response = web.Response(status=200)
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    return middleware


def _node_detail_response(graph, node_id: str, *, include_embedding: bool = False) -> web.Response:
    node = graph.get_node(node_id)
    if not node:
        raise web.HTTPNotFound(text=f"Node not found: {node_id}")

    node_data = node.to_dict()
    if include_embedding:
        node_data["has_embedding"] = node.embedding is not None
        node_data["embedding_dim"] = len(node.embedding) if node.embedding else 0

    return web.json_response({
        "node": node_data,
        "outgoing_edges": [edge.to_dict() for edge in graph.get_outgoing_edges(node_id)],
        "incoming_edges": [edge.to_dict() for edge in graph.get_incoming_edges(node_id)],
        "children": [child.to_dict() for child in graph.get_children(node_id)],
        "parent": graph.get_parent(node_id).to_dict() if graph.get_parent(node_id) else None,
    })
