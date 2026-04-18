"""HTTP server bootstrap."""

from __future__ import annotations

import logging

from aiohttp import web

from mimir.container import Container
from mimir.domain.config import MimirConfig
from mimir.services.repo_sync import RepoSyncQueue

from .routes_admin import register_admin_routes
from .routes_api import register_api_routes
from .routes_mcp import register_mcp_routes
from .state import HttpServerState

logger = logging.getLogger(__name__)


def run_http_server(
    config: MimirConfig,
    host: str = "0.0.0.0",
    port: int = 8421,
    workspace_name: str | None = None,
) -> None:
    container = Container(config)
    state = HttpServerState(
        container=container,
        workspace_name=workspace_name or "default",
        graph=container.load_graph(),
    )
    sync_queue = (
        RepoSyncQueue(
            container.repo_sync,
            state.refresh_repo,
            history_limit=config.admin.job_history_limit,
        )
        if config.admin.enable_repo_sync else None
    )

    logger.info(
        "HTTP server starting — workspace=%s, host=%s, port=%d, graph=%d nodes",
        state.workspace_name,
        host,
        port,
        state.graph.node_count,
    )

    app = _build_app(state, sync_queue)
    container.warmup()

    logger.info("Shared Mimir HTTP server listening on http://%s:%d", host, port)
    web.run_app(app, host=host, port=port, print=lambda _: None, access_log=None)


def _build_app(state: HttpServerState, sync_queue: RepoSyncQueue | None) -> web.Application:
    routes = web.RouteTableDef()
    register_api_routes(routes, state)
    register_admin_routes(routes, state, sync_queue)
    register_mcp_routes(routes, state)

    app = web.Application(middlewares=[_cors_middleware])
    app.router.add_routes(routes)
    app.on_cleanup.append(_cleanup_factory(state, sync_queue))
    if sync_queue is not None:
        app.on_startup.append(_startup_factory(sync_queue))
    return app


def _startup_factory(sync_queue: RepoSyncQueue):
    async def on_startup(app: web.Application) -> None:
        sync_queue.start()

    return on_startup


def _cleanup_factory(state: HttpServerState, sync_queue: RepoSyncQueue | None):
    async def on_cleanup(app: web.Application) -> None:
        if sync_queue is not None:
            await sync_queue.stop()
        state.container.close()

    return on_cleanup


@web.middleware
async def _cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response
