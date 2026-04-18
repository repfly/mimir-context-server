"""Primary HTTP JSON endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from mimir.adapters.http.admin_auth import require_admin_token
from mimir.adapters.shared.session_context import apply_session_context
from mimir.adapters.http.state import HttpServerState
from mimir.domain.guardrails_config import load_rules

logger = logging.getLogger(__name__)


def register_api_routes(routes: web.RouteTableDef, state: HttpServerState) -> None:
    @routes.get("/api/v1/health")
    async def health(request: web.Request) -> web.Response:
        graph = state.current_graph()
        return web.json_response({
            "status": "ok",
            "workspace": state.workspace_name,
            "graph_nodes": graph.node_count,
            "graph_edges": graph.edge_count,
        })

    @routes.post("/api/v1/context")
    async def api_context(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        query = body.get("query")
        if not query:
            return web.json_response({"error": "Missing 'query' field"}, status=400)

        graph = state.current_graph()
        try:
            bundle = await state.container.retrieval.search(
                query=query,
                graph=graph,
                token_budget=body.get("budget"),
                repos=body.get("repos"),
            )
            apply_session_context(
                state.container,
                bundle,
                query=query,
                session_id=body.get("session_id"),
                budget=body.get("budget"),
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
        stats = state.current_graph().stats()
        stats["workspace"] = state.workspace_name
        return web.json_response(stats)

    @routes.get("/api/v1/hotspots")
    async def api_hotspots(request: web.Request) -> web.Response:
        graph = state.current_graph()
        top_n = int(request.query.get("top", "20"))
        results = state.container.temporal.get_hotspots(graph, top_n=top_n)
        return web.json_response([
            {"node": n.id, "score": round(s, 4), "changes": n.modification_count}
            for n, s in results
        ])

    @routes.get("/api/v1/quality")
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

    @routes.get("/api/v1/catalog")
    async def api_catalog(request: web.Request) -> web.Response:
        graph = state.current_graph()
        try:
            repos_param = request.query.get("repos")
            response = state.container.catalog.generate_catalog(
                graph,
                repos=repos_param.split(",") if repos_param else None,
            )
            return web.json_response(response.to_dict())
        except Exception as exc:
            logger.error("Catalog generation failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.get("/api/v1/catalog/{repo}")
    async def api_catalog_service(request: web.Request) -> web.Response:
        graph = state.current_graph()
        repo = request.match_info["repo"]
        try:
            response = state.container.catalog.generate_catalog(graph, repos=[repo])
            if not response.services:
                return web.json_response({"error": f"Repo '{repo}' not found in graph"}, status=404)
            return web.json_response(response.services[0].to_dict())
        except Exception as exc:
            logger.error("Catalog single-service failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/api/v1/catalog/drift")
    async def api_catalog_drift(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        repo = body.get("repo")
        if not repo:
            return web.json_response({"error": "Missing 'repo' field"}, status=400)

        graph = state.current_graph()
        try:
            report = state.container.catalog.detect_drift(
                graph,
                repo,
                body.get("declared_dependencies", []),
            )
            return web.json_response(report.to_dict())
        except Exception as exc:
            logger.error("Drift detection failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/api/v1/guardrails/check")
    async def api_guardrails_check(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        diff = body.get("diff")
        if not diff:
            return web.json_response({"error": "Missing 'diff' field"}, status=400)

        try:
            rules = load_rules(Path(body.get("rules_path", "mimir-rules.yaml")))
        except Exception as exc:
            return web.json_response({"error": f"Rule loading failed: {exc}"}, status=400)

        graph = state.current_graph()
        try:
            result = await state.container.guardrail.evaluate(graph, diff, rules)
            return web.json_response(result.to_dict())
        except Exception as exc:
            logger.error("Guardrail check failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/api/v1/clear")
    async def api_clear(request: web.Request) -> web.Response:
        require_admin_token(request, state.container.config)
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = await state.clear(
            graph=body.get("graph", True),
            sessions=body.get("sessions", True),
        )
        return web.json_response(result)
