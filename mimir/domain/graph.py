"""In-memory code graph backed by NetworkX.

``CodeGraph`` is the central data structure of Mimir.  It wraps a
``networkx.DiGraph`` and provides typed, domain-aware accessors.
No I/O operations are performed inside this module.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable, Iterator, Optional, Sequence

import networkx as nx

from mimir.domain.models import (
    CONTAINER_KINDS,
    CROSS_REPO_EDGE_KINDS,
    SYMBOL_KINDS,
    Edge,
    EdgeKind,
    Node,
    NodeKind,
)

logger = logging.getLogger(__name__)


class CodeGraph:
    """Unified code graph supporting multi-repo, typed nodes and edges.

    Internally delegates to ``networkx.DiGraph`` for traversal and
    algorithm support while keeping a domain-typed API surface.
    """

    def __init__(self) -> None:
        self._g = nx.DiGraph()
        self._nodes: dict[str, Node] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add_node(self, node: Node) -> None:
        """Insert or replace a node."""
        self._nodes[node.id] = node
        self._g.add_node(node.id)

    def add_edge(self, edge: Edge) -> None:
        """Insert an edge.  Both endpoints must already be registered."""
        if edge.source not in self._nodes:
            logger.warning("add_edge: source %s not in graph, skipping", edge.source)
            return
        if edge.target not in self._nodes:
            logger.warning("add_edge: target %s not in graph, skipping", edge.target)
            return
        self._g.add_edge(
            edge.source,
            edge.target,
            kind=edge.kind,
            weight=edge.weight,
            metadata=edge.metadata,
        )

    def remove_node(self, node_id: str) -> None:
        """Remove a node and all its incident edges."""
        self._nodes.pop(node_id, None)
        if self._g.has_node(node_id):
            self._g.remove_node(node_id)

    def remove_edge(self, source: str, target: str) -> None:
        if self._g.has_edge(source, target):
            self._g.remove_edge(source, target)

    def remove_nodes_by_repo(self, repo: str) -> list[str]:
        """Remove all nodes belonging to a repo.  Returns removed node IDs."""
        to_remove = [nid for nid, n in self._nodes.items() if n.repo == repo]
        for nid in to_remove:
            self.remove_node(nid)
        return to_remove

    def remove_nodes_by_paths(self, repo: str, rel_paths: set[str]) -> list[str]:
        """Remove nodes whose ``path`` matches any of *rel_paths* within *repo*.

        For each file path, this also removes the file node and every symbol
        node underneath it.  Module nodes are kept (they can be shared across
        files).  Returns the IDs of removed nodes.
        """
        to_remove: list[str] = []
        for nid, node in list(self._nodes.items()):
            if node.repo != repo:
                continue
            if node.kind in (NodeKind.FILE,) and node.path in rel_paths:
                to_remove.append(nid)
            elif node.is_symbol and node.path in rel_paths:
                to_remove.append(nid)
        for nid in to_remove:
            self.remove_node(nid)
        return to_remove

    def clear(self) -> None:
        self._g.clear()
        self._nodes.clear()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def has_edge(self, source: str, target: str) -> bool:
        return self._g.has_edge(source, target)

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def all_nodes(self) -> Iterator[Node]:
        yield from self._nodes.values()

    def all_edges(self) -> Iterator[Edge]:
        for u, v, data in self._g.edges(data=True):
            yield Edge(
                source=u,
                target=v,
                kind=data["kind"],
                weight=data.get("weight", 1.0),
                metadata=data.get("metadata", {}),
            )

    def nodes_by_repo(self, repo: str) -> Iterator[Node]:
        for node in self._nodes.values():
            if node.repo == repo:
                yield node

    def nodes_by_kind(self, kind: NodeKind) -> Iterator[Node]:
        for node in self._nodes.values():
            if node.kind == kind:
                yield node

    def symbol_nodes(self) -> Iterator[Node]:
        for node in self._nodes.values():
            if node.kind in SYMBOL_KINDS:
                yield node

    def container_nodes(self) -> Iterator[Node]:
        for node in self._nodes.values():
            if node.kind in CONTAINER_KINDS:
                yield node

    # ------------------------------------------------------------------
    # Edge queries
    # ------------------------------------------------------------------

    def get_outgoing_edges(
        self,
        node_id: str,
        kind: Optional[EdgeKind] = None,
    ) -> list[Edge]:
        """Edges leaving *node_id*, optionally filtered by kind."""
        if not self._g.has_node(node_id):
            return []
        edges: list[Edge] = []
        for _, target, data in self._g.out_edges(node_id, data=True):
            if kind is not None and data["kind"] != kind:
                continue
            edges.append(Edge(
                source=node_id,
                target=target,
                kind=data["kind"],
                weight=data.get("weight", 1.0),
                metadata=data.get("metadata", {}),
            ))
        return edges

    def get_incoming_edges(
        self,
        node_id: str,
        kind: Optional[EdgeKind] = None,
    ) -> list[Edge]:
        """Edges arriving at *node_id*, optionally filtered by kind."""
        if not self._g.has_node(node_id):
            return []
        edges: list[Edge] = []
        for source, _, data in self._g.in_edges(node_id, data=True):
            if kind is not None and data["kind"] != kind:
                continue
            edges.append(Edge(
                source=source,
                target=node_id,
                kind=data["kind"],
                weight=data.get("weight", 1.0),
                metadata=data.get("metadata", {}),
            ))
        return edges

    def get_all_edges_for(
        self,
        node_id: str,
        kind: Optional[EdgeKind] = None,
    ) -> list[Edge]:
        """All edges incident to *node_id* (both directions)."""
        return (
            self.get_outgoing_edges(node_id, kind)
            + self.get_incoming_edges(node_id, kind)
        )

    # ------------------------------------------------------------------
    # Containment hierarchy
    # ------------------------------------------------------------------

    def get_children(self, node_id: str) -> list[Node]:
        """Direct children via CONTAINS edges."""
        edges = self.get_outgoing_edges(node_id, EdgeKind.CONTAINS)
        return [self._nodes[e.target] for e in edges if e.target in self._nodes]

    def get_parent(self, node_id: str) -> Optional[Node]:
        """Direct parent via incoming CONTAINS edge."""
        edges = self.get_incoming_edges(node_id, EdgeKind.CONTAINS)
        if edges:
            return self._nodes.get(edges[0].source)
        return None

    def get_descendants(self, node_id: str) -> list[Node]:
        """All transitive children via CONTAINS edges."""
        result: list[Node] = []
        stack = [node_id]
        visited: set[str] = set()
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            for child in self.get_children(current):
                result.append(child)
                stack.append(child.id)
        return result

    # ------------------------------------------------------------------
    # Call graph
    # ------------------------------------------------------------------

    def get_callees(self, node_id: str) -> list[Node]:
        """Functions called by *node_id*."""
        edges = self.get_outgoing_edges(node_id, EdgeKind.CALLS)
        return [self._nodes[e.target] for e in edges if e.target in self._nodes]

    def get_callers(self, node_id: str) -> list[Node]:
        """Functions that call *node_id*."""
        edges = self.get_incoming_edges(node_id, EdgeKind.CALLS)
        return [self._nodes[e.source] for e in edges if e.source in self._nodes]

    # ------------------------------------------------------------------
    # Cross-repo
    # ------------------------------------------------------------------

    def cross_repo_edges(self) -> list[Edge]:
        """All edges that span repository boundaries."""
        result: list[Edge] = []
        for edge in self.all_edges():
            if edge.kind in CROSS_REPO_EDGE_KINDS:
                result.append(edge)
                continue
            src = self._nodes.get(edge.source)
            tgt = self._nodes.get(edge.target)
            if src and tgt and src.repo != tgt.repo:
                result.append(edge)
        return result

    # ------------------------------------------------------------------
    # Shortest path
    # ------------------------------------------------------------------

    def shortest_path(self, from_id: str, to_id: str) -> list[Edge]:
        """Find the shortest dependency path between two nodes.

        Returns the list of edges along the path, or an empty list if
        no path exists.
        """
        try:
            node_path: list[str] = nx.shortest_path(self._g, from_id, to_id)
        except (nx.NodeNotFound, nx.NetworkXNoPath):
            return []

        edges: list[Edge] = []
        for u, v in zip(node_path, node_path[1:]):
            data = self._g.edges[u, v]
            edges.append(Edge(
                source=u,
                target=v,
                kind=data["kind"],
                weight=data.get("weight", 1.0),
                metadata=data.get("metadata", {}),
            ))
        return edges

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return self._g.number_of_edges()

    @property
    def repos(self) -> list[str]:
        return sorted({n.repo for n in self._nodes.values()})

    def stats(self) -> dict:
        """Aggregate statistics about the graph."""
        nodes_by_kind: dict[str, int] = defaultdict(int)
        nodes_by_repo: dict[str, int] = defaultdict(int)
        edges_by_kind: dict[str, int] = defaultdict(int)

        for node in self._nodes.values():
            nodes_by_kind[node.kind.value] += 1
            nodes_by_repo[node.repo] += 1

        for _, _, data in self._g.edges(data=True):
            edges_by_kind[data["kind"].value] += 1

        return {
            "total_nodes": len(self._nodes),
            "total_edges": self._g.number_of_edges(),
            "repos": self.repos,
            "nodes_by_kind": dict(nodes_by_kind),
            "nodes_by_repo": dict(nodes_by_repo),
            "edges_by_kind": dict(edges_by_kind),
        }

    # ------------------------------------------------------------------
    # Nodes at specific hierarchy levels (for beam search)
    # ------------------------------------------------------------------

    def nodes_at_level(self, kind: NodeKind, *, repo: Optional[str] = None) -> list[Node]:
        """Return all nodes of a given kind, optionally filtered by repo."""
        result: list[Node] = []
        for node in self._nodes.values():
            if node.kind != kind:
                continue
            if repo is not None and node.repo != repo:
                continue
            result.append(node)
        return result

    def __repr__(self) -> str:
        return f"CodeGraph(nodes={self.node_count}, edges={self.edge_count})"
