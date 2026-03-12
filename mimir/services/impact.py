"""Impact analysis — reverse-trace dependencies to find what breaks."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from mimir.domain.graph import CodeGraph
from mimir.domain.models import EdgeKind, Node, NodeKind
from mimir.services._conventions import find_test_file


_DEPENDENCY_EDGES = frozenset({
    EdgeKind.CALLS,
    EdgeKind.USES_TYPE,
    EdgeKind.INHERITS,
    EdgeKind.IMPLEMENTS,
    EdgeKind.IMPORTS,
})


@dataclass
class ImpactResult:
    """Result of an impact analysis."""

    target: Node
    direct_callers: list[Node]
    type_users: list[Node]
    implementors: list[Node]
    test_files: list[Node]
    transitive: dict[int, list[Node]]
    total_impact_count: int

    def format_for_llm(self) -> str:
        parts: list[str] = []
        parts.append(f"## Impact analysis for `{self.target.name}`")
        if self.target.path:
            parts.append(f"File: `{self.target.path}`")
        parts.append(f"Total impacted nodes: {self.total_impact_count}\n")

        if self.direct_callers:
            parts.append("### Direct callers")
            for n in self.direct_callers:
                loc = f" ({n.path})" if n.path else ""
                parts.append(f"- `{n.name}`{loc}")
            parts.append("")

        if self.type_users:
            parts.append("### Type users")
            for n in self.type_users:
                loc = f" ({n.path})" if n.path else ""
                parts.append(f"- `{n.name}`{loc}")
            parts.append("")

        if self.implementors:
            parts.append("### Implementors / subclasses")
            for n in self.implementors:
                loc = f" ({n.path})" if n.path else ""
                parts.append(f"- `{n.name}`{loc}")
            parts.append("")

        if self.test_files:
            parts.append("### Test files")
            for n in self.test_files:
                parts.append(f"- `{n.path}`")
            parts.append("")

        if self.transitive:
            parts.append("### Transitive impact (by hop distance)")
            for hop in sorted(self.transitive.keys()):
                nodes = self.transitive[hop]
                parts.append(f"**Hop {hop}** ({len(nodes)} nodes):")
                for n in nodes[:15]:
                    loc = f" ({n.path})" if n.path else ""
                    parts.append(f"  - `{n.name}`{loc}")
                if len(nodes) > 15:
                    parts.append(f"  - ... and {len(nodes) - 15} more")
            parts.append("")

        return "\n".join(parts) if parts else "No impact found."


class ImpactService:
    """Analyzes what would be affected by changing a symbol."""

    def analyze(
        self,
        graph: CodeGraph,
        *,
        node_id: Optional[str] = None,
        file_path: Optional[str] = None,
        symbol_name: Optional[str] = None,
        max_hops: int = 3,
    ) -> Optional[ImpactResult]:
        """Run impact analysis on a target node.

        Resolve target from node_id, or file_path + symbol_name.
        """
        target = self._resolve_target(graph, node_id, file_path, symbol_name)
        if not target:
            return None

        # Direct impact: incoming edges
        direct_callers: list[Node] = []
        type_users: list[Node] = []
        implementors: list[Node] = []

        for edge in graph.get_incoming_edges(target.id, EdgeKind.CALLS):
            caller = graph.get_node(edge.source)
            if caller:
                direct_callers.append(caller)

        for edge in graph.get_incoming_edges(target.id, EdgeKind.USES_TYPE):
            user = graph.get_node(edge.source)
            if user:
                type_users.append(user)

        for edge_kind in (EdgeKind.INHERITS, EdgeKind.IMPLEMENTS):
            for edge in graph.get_incoming_edges(target.id, edge_kind):
                impl = graph.get_node(edge.source)
                if impl:
                    implementors.append(impl)

        # Test files
        test_files: list[Node] = []
        if target.path:
            test = find_test_file(target.path, graph)
            if test:
                test_files.append(test)

        # Transitive impact: BFS on incoming dependency edges
        transitive: dict[int, list[Node]] = defaultdict(list)
        visited: set[str] = {target.id}
        # Hop 1 is the direct callers/users already collected
        frontier = {n.id for n in direct_callers + type_users + implementors}
        visited |= frontier

        for hop in range(2, max_hops + 1):
            next_frontier: set[str] = set()
            for nid in frontier:
                for edge_kind in _DEPENDENCY_EDGES:
                    for edge in graph.get_incoming_edges(nid, edge_kind):
                        if edge.source not in visited:
                            node = graph.get_node(edge.source)
                            if node:
                                transitive[hop].append(node)
                                next_frontier.add(edge.source)
                                visited.add(edge.source)
            frontier = next_frontier

        total = (
            len(direct_callers) + len(type_users) + len(implementors)
            + sum(len(v) for v in transitive.values())
        )

        return ImpactResult(
            target=target,
            direct_callers=direct_callers,
            type_users=type_users,
            implementors=implementors,
            test_files=test_files,
            transitive=dict(transitive),
            total_impact_count=total,
        )

    @staticmethod
    def _resolve_target(
        graph: CodeGraph,
        node_id: Optional[str],
        file_path: Optional[str],
        symbol_name: Optional[str],
    ) -> Optional[Node]:
        """Resolve target node from various identifiers."""
        if node_id:
            return graph.get_node(node_id)

        if file_path and symbol_name:
            for node in graph.all_nodes():
                if not node.is_symbol:
                    continue
                if node.path and node.path.endswith(file_path) and node.name == symbol_name:
                    return node

        if symbol_name:
            # Fallback: match by name only
            for node in graph.all_nodes():
                if node.is_symbol and node.name == symbol_name:
                    return node

        if file_path:
            # Return the file node itself
            for node in graph.all_nodes():
                if node.kind == NodeKind.FILE and node.path and node.path.endswith(file_path):
                    return node

        return None
