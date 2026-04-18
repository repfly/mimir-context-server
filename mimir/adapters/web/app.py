"""Web inspector bootstrap."""

from __future__ import annotations

import logging

from aiohttp import web

from mimir.container import Container
from mimir.domain.config import MimirConfig

from .routes import cors_middleware, register_routes
from .state import WebServerState

logger = logging.getLogger(__name__)


def run_web_server(config: MimirConfig, port: int = 8420) -> None:
    """Start the aiohttp web inspector."""
    container = Container(config)
    state = WebServerState(container=container, graph=container.load_graph())

    routes = web.RouteTableDef()
    register_routes(routes, state)

    app = web.Application(middlewares=[cors_middleware()])
    app.router.add_routes(routes)

    logger.info("Starting web inspector on port %d", port)
    web.run_app(app, host="0.0.0.0", port=port, print=lambda _: None, access_log=None)
