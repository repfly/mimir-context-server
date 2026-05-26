"""Primary HTTP JSON endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from mimir.adapters.shared.session_context import apply_session_context
from mimir.adapters.http.state import HttpServerState
from mimir.domain.errors import NoActiveIndexError
from mimir.domain.feedback import FeedbackOutcome
from mimir.domain.guardrails_config import load_rules

logger = logging.getLogger(__name__)


def no_active_index_response() -> web.Response:
    return web.json_response(
        {"error": "No active index", "hint": "Run `mimir indexer run` first."},
        status=503,
    )


def register_api_routes(routes: web.RouteTableDef, state: HttpServerState) -> None:
    @routes.get("/api/v1/health")
    async def health(request: web.Request) -> web.Response:
        graph = state.current_graph()
        return web.json_response({
            "status": "ok",
            "workspace": state.workspace_name,
            "active_version": state.runtime.active_version.version if state.runtime.active_version else None,
            "graph_nodes": graph.node_count,
            "graph_edges": graph.edge_count,
        })

    @routes.get("/api/v1/index/status")
    async def index_status(request: web.Request) -> web.Response:
        status = state.runtime.status()
        status["workspace"] = state.workspace_name
        return web.json_response(status)

    @routes.post("/api/v1/context")
    async def api_context(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        query = body.get("query")
        if not query:
            return web.json_response({"error": "Missing 'query' field"}, status=400)

        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        try:
            bundle = await state.runtime.retrieval.search(
                query=query,
                graph=graph,
                token_budget=body.get("budget"),
                repos=body.get("repos"),
            )
            apply_session_context(
                state.runtime,
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
        try:
            stats = state.runtime.require_graph().stats()
        except NoActiveIndexError:
            return no_active_index_response()
        stats["workspace"] = state.workspace_name
        return web.json_response(stats)

    @routes.get("/api/v1/hotspots")
    async def api_hotspots(request: web.Request) -> web.Response:
        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        top_n = int(request.query.get("top", "20"))
        results = state.runtime.temporal.get_hotspots(graph, top_n=top_n)
        return web.json_response([
            {"node": n.id, "score": round(s, 4), "changes": n.modification_count}
            for n, s in results
        ])

    @routes.post("/api/v1/write_context")
    async def api_write_context(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        file_path = body.get("file_path")
        if not file_path:
            return web.json_response({"error": "Missing 'file_path' field"}, status=400)
        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        bundle = state.runtime.write_context.assemble(file_path=file_path, graph=graph)
        return web.json_response({"formatted": bundle.format_for_llm()})

    @routes.post("/api/v1/impact")
    async def api_impact(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        result = state.runtime.impact.analyze(
            graph,
            node_id=body.get("node_id"),
            file_path=body.get("file_path"),
            symbol_name=body.get("symbol_name"),
            max_hops=body.get("max_hops", 3),
        )
        if result is None:
            return web.json_response({"error": "No matching symbol or file found"}, status=404)
        return web.json_response({"formatted": result.format_for_llm()})

    @routes.get("/api/v1/quality")
    async def api_quality(request: web.Request) -> web.Response:
        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        repos_param = request.query.get("repos")
        overview = state.runtime.quality.detect_gaps(
            graph,
            repos=repos_param.split(",") if repos_param else None,
            threshold=float(request.query.get("threshold", "0.3")),
            top_n=int(request.query.get("top_n", "50")),
        )
        return web.json_response(overview.to_dict())

    @routes.get("/api/v1/catalog")
    async def api_catalog(request: web.Request) -> web.Response:
        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        try:
            repos_param = request.query.get("repos")
            response = state.runtime.catalog.generate_catalog(
                graph,
                repos=repos_param.split(",") if repos_param else None,
            )
            return web.json_response(response.to_dict())
        except Exception as exc:
            logger.error("Catalog generation failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.get("/api/v1/catalog/{repo}")
    async def api_catalog_service(request: web.Request) -> web.Response:
        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        repo = request.match_info["repo"]
        try:
            response = state.runtime.catalog.generate_catalog(graph, repos=[repo])
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

        try:
            graph = state.runtime.require_graph()
        except NoActiveIndexError:
            return no_active_index_response()
        try:
            report = state.runtime.catalog.detect_drift(
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
            graph = state.runtime.require_graph()
            rules = load_rules(Path(body.get("rules_path", "mimir-rules.yaml")))
            result = await state.runtime.guardrail.evaluate(graph, diff, rules)
            return web.json_response(result.to_dict())
        except NoActiveIndexError:
            return no_active_index_response()
        except Exception as exc:
            logger.error("Guardrail check failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.post("/api/v1/feedback")
    async def api_feedback(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        node_ids = body.get("node_ids")
        outcome_raw = body.get("outcome")
        try:
            outcome = FeedbackOutcome(outcome_raw) if outcome_raw else None
        except ValueError:
            outcome = None
        if not node_ids or outcome is None:
            return web.json_response(
                {"error": "Missing 'node_ids' (list) or invalid 'outcome' (positive|negative)"},
                status=400,
            )

        try:
            signal = state.runtime.feedback.record_explicit(
                node_ids=node_ids,
                outcome=outcome,
                session_id=body.get("session_id"),
                query=body.get("query"),
            )
            return web.json_response(signal.to_dict())
        except Exception as exc:
            logger.error("Feedback recording failed: %s", exc, exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    @routes.get("/api/v1/feedback/stats")
    async def api_feedback_stats(request: web.Request) -> web.Response:
        node_id = request.query.get("node_id")
        if not node_id:
            return web.json_response({"error": "Missing 'node_id' query param"}, status=400)

        score = state.runtime.feedback.get_node_score(node_id)
        if score is None:
            return web.json_response({"node_id": node_id, "score": 0.5, "status": "no_data"})
        return web.json_response(score.to_dict())
