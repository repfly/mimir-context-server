"""HTTP server bootstrap."""

from __future__ import annotations

import logging

from aiohttp import web

from mimir.domain.config import MimirConfig
from mimir.runtime import RuntimeFactory

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
    runtime = RuntimeFactory(config).query()
    state = HttpServerState(
        runtime=runtime,
        workspace_name=workspace_name or "default",
    )

    logger.info(
        "HTTP server starting — workspace=%s, host=%s, port=%d, graph=%d nodes",
        state.workspace_name,
        host,
        port,
        state.current_graph().node_count,
    )

    app = _build_app(state)

    logger.info("Mimir query HTTP server listening on http://%s:%d", host, port)
    web.run_app(app, host=host, port=port, print=lambda _: None, access_log=None)


def _build_app(state: HttpServerState) -> web.Application:
    routes = web.RouteTableDef()
    register_api_routes(routes, state)
    register_mcp_routes(routes, state)

    app = web.Application(middlewares=[_cors_middleware])
    app.router.add_routes(routes)
    app.on_startup.append(_startup_factory(state))
    app.on_cleanup.append(_cleanup_factory(state))
    return app


def _startup_factory(state: HttpServerState):
    async def on_startup(app: web.Application) -> None:
        state.runtime.start_polling()

    return on_startup


def _cleanup_factory(state: HttpServerState):
    async def on_cleanup(app: web.Application) -> None:
        await state.runtime.stop_polling()
        state.runtime.close()

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
