"""Web inspector bootstrap."""

from __future__ import annotations

import logging

from aiohttp import web

from mimir.domain.config import MimirConfig
from mimir.runtime import RuntimeFactory

from .routes import cors_middleware, register_routes
from .state import WebServerState

logger = logging.getLogger(__name__)


def run_web_server(config: MimirConfig, port: int = 8420) -> None:
    """Start the aiohttp web inspector."""
    runtime = RuntimeFactory(config).query()
    state = WebServerState(runtime=runtime)

    routes = web.RouteTableDef()
    register_routes(routes, state)

    app = web.Application(middlewares=[cors_middleware()])
    app.router.add_routes(routes)
    app.on_cleanup.append(_cleanup_factory(state))

    logger.info("Starting web inspector on port %d", port)
    web.run_app(app, host="0.0.0.0", port=port, print=lambda _: None, access_log=None)


def _cleanup_factory(state: WebServerState):
    async def on_cleanup(app: web.Application) -> None:
        state.runtime.close()

    return on_cleanup
