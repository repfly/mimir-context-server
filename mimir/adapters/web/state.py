"""Mutable state for the web inspector adapter."""

from __future__ import annotations

from dataclasses import dataclass

from mimir.container import Container


@dataclass
class WebServerState:
    container: Container
    graph: object

    def current_graph(self):
        return self.graph

    def reload_graph(self) -> object:
        self.graph = self.container.load_graph(force_reload=True)
        return self.graph
