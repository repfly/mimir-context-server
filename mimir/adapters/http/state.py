"""Shared request/application state for the HTTP adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from mimir.container import Container


@dataclass
class HttpServerState:
    """Mutable server state shared across route modules."""

    container: Container
    workspace_name: str
    graph: object
    _graph_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def current_graph(self):
        return self.graph

    async def refresh_repo(self, repo_name: str) -> dict:
        async with self._graph_update_lock:
            working_graph = self.container.graph_store.load()
            result = await self.container.indexing.refresh_repo(working_graph, repo_name)
            self.container.replace_graph(working_graph)
            self.graph = working_graph
            self.container.retrieval.invalidate_bm25()
            return result

    def reload_graph(self, *, force_reload: bool = False) -> object:
        self.graph = self.container.load_graph(force_reload=force_reload)
        return self.graph

    async def clear(self, *, graph: bool = True, sessions: bool = True) -> dict:
        async with self._graph_update_lock:
            result = self.container.clear_data(graph=graph, sessions=sessions)
            if graph:
                self.reload_graph(force_reload=True)
                self.container.retrieval.invalidate_bm25()
            return result
