"""SubGraph and ContextBundle — query result containers.

A ``SubGraph`` is a subset of the full ``CodeGraph`` that captures the result
of a context assembly operation.  ``ContextBundle`` is the final, LLM-ready
output including metadata and session notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mimir.domain.models import Edge, Node


@dataclass
class SubGraph:
    """A subset of the code graph assembled for a specific query.

    Unlike ``CodeGraph``, this is a lightweight container — no NetworkX
    dependency, just lists of nodes and edges with helper methods.
    """

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def add_node(self, node: Node, *, score: float = 0.0) -> None:
        self.nodes[node.id] = node
        if score > 0:
            self.scores[node.id] = score

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    def remove_node(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        self.scores.pop(node_id, None)
        self.edges = [
            e for e in self.edges
            if e.source != node_id and e.target != node_id
        ]

    def has_node(self, node_id: str) -> bool:
        return node_id in self.nodes

    @property
    def node_list(self) -> list[Node]:
        return list(self.nodes.values())

    @property
    def node_ids(self) -> set[str]:
        return set(self.nodes.keys())

    @property
    def token_estimate(self) -> int:
        return sum(n.token_estimate for n in self.nodes.values())

    @property
    def repos_involved(self) -> list[str]:
        return sorted({n.repo for n in self.nodes.values()})

    def leaf_nodes(self) -> list[Node]:
        """Nodes with no outgoing edges within this subgraph."""
        sources = {e.source for e in self.edges}
        targets = {e.target for e in self.edges}
        return [
            n for n in self.nodes.values()
            if n.id not in sources or n.id not in self.nodes
        ]

    def __repr__(self) -> str:
        return f"SubGraph(nodes={len(self.nodes)}, edges={len(self.edges)})"


@dataclass
class ContextBundle:
    """Final, LLM-ready output of a context assembly operation.

    This is what the MCP server returns to the IDE / LLM.
    """

    nodes: list[Node]
    edges: list[Edge]
    summary: str
    token_count: int
    repos_involved: list[str]
    session_note: Optional[str] = None
    seed_ids: list[str] = field(default_factory=list)

    def format_for_llm(self) -> str:
        """Render the context bundle as structured text for an LLM."""
        parts: list[str] = []

        # Header
        if self.repos_involved:
            parts.append(f"**Repos involved:** {', '.join(self.repos_involved)}")
        if self.session_note:
            parts.append(f"**Note:** {self.session_note}")
        parts.append("")

        # Group nodes by repo
        by_repo: dict[str, list[Node]] = {}
        for node in self.nodes:
            by_repo.setdefault(node.repo, []).append(node)

        for repo, repo_nodes in by_repo.items():
            parts.append(f"### {repo}")
            parts.append("")

            for node in repo_nodes:
                location = f"{repo}:{node.path}" if node.path else repo
                if node.start_line is not None:
                    location += f" (lines {node.start_line}-{node.end_line})"

                # Dependency annotations
                annotations: list[str] = []
                for edge in self.edges:
                    if edge.source == node.id:
                        annotations.append(f"{edge.kind.value}: {edge.target.split('::')[-1]}")
                    elif edge.target == node.id:
                        annotations.append(f"CALLED BY: {edge.source.split('::')[-1]}")

                parts.append(f"```python")
                parts.append(f"# {location}")
                if annotations:
                    parts.append(f"# {' | '.join(annotations[:5])}")
                if node.raw_code:
                    parts.append(node.raw_code)
                elif node.summary:
                    parts.append(f"# Summary: {node.summary}")
                parts.append("```")
                parts.append("")

        return "\n".join(parts)
