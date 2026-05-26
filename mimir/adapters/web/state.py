"""State for the read-only web inspector adapter."""

from __future__ import annotations

from dataclasses import dataclass

from mimir.runtime import QueryRuntime


@dataclass
class WebServerState:
    runtime: QueryRuntime

    def current_graph(self):
        return self.runtime.graph
