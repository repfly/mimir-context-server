"""Shared request/application state for the query HTTP adapter."""

from __future__ import annotations

from dataclasses import dataclass

from mimir.runtime import QueryRuntime


@dataclass
class HttpServerState:
    """Mutable server state shared across route modules."""

    runtime: QueryRuntime
    workspace_name: str

    def current_graph(self):
        return self.runtime.graph
