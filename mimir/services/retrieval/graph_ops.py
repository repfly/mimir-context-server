"""Subgraph assembly and metadata helpers for retrieval."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from mimir.domain.config import MimirConfig
from mimir.domain.graph import CodeGraph
from mimir.domain.models import EDGE_EXPANSION_WEIGHTS, EdgeKind, Node, NodeKind
from mimir.domain.subgraph import SubGraph


class RetrievalGraphOps:
    """Owns subgraph expansion, pruning, ordering, and metadata updates."""

    def __init__(
        self,
        config: MimirConfig,
        *,
        quality_service=None,
        temporal_service=None,
        graph_store=None,
    ) -> None:
        self._config = config
        self._quality_service = quality_service
        self._temporal_service = temporal_service
        self._graph_store = graph_store

    def expand_subgraph(
        self,
        seeds: list[Node],
        seed_scores: dict[str, float],
        query_embedding: list[float],
        graph: CodeGraph,
        *,
        hops: Optional[int] = None,
        gate: Optional[float] = None,
    ) -> SubGraph:
        hops = hops if hops is not None else self._config.retrieval.expansion_hops
        gate = gate if gate is not None else self._config.retrieval.relevance_gate

        subgraph = SubGraph()
        for seed in seeds:
            subgraph.add_node(seed.clone(), score=seed_scores.get(seed.id, 1.0))

        frontier = {seed.id for seed in seeds}
        visited: set[str] = set()
        for _ in range(hops):
            next_frontier: set[str] = set()
            for node_id in frontier:
                if node_id in visited:
                    continue
                visited.add(node_id)

                for edge in graph.get_all_edges_for(node_id):
                    target_id = edge.target if edge.source == node_id else edge.source
                    if target_id in visited or target_id in subgraph.node_ids:
                        if subgraph.has_node(target_id):
                            subgraph.add_edge(edge)
                        continue

                    target = graph.get_node(target_id)
                    if target is None:
                        continue

                    if target.embedding and query_embedding:
                        similarity = self.cosine_similarity(query_embedding, target.embedding)
                        if similarity < gate:
                            continue
                    else:
                        similarity = 0.5

                    score = similarity * EDGE_EXPANSION_WEIGHTS.get(edge.kind, 0.5)
                    subgraph.add_node(target.clone(), score=score)
                    subgraph.add_edge(edge)
                    next_frontier.add(target_id)

            frontier = next_frontier

        return subgraph

    @staticmethod
    def add_type_context(subgraph: SubGraph, graph: CodeGraph) -> None:
        for node in list(subgraph.nodes.values()):
            for edge in graph.get_outgoing_edges(node.id, EdgeKind.USES_TYPE):
                type_node = graph.get_node(edge.target)
                if type_node is not None and not subgraph.has_node(type_node.id):
                    subgraph.add_node(type_node.clone(), score=0.3)
                    subgraph.add_edge(edge)

    @staticmethod
    def add_config_context(subgraph: SubGraph, graph: CodeGraph) -> None:
        for node in list(subgraph.nodes.values()):
            for edge in graph.get_outgoing_edges(node.id, EdgeKind.READS_CONFIG):
                cfg_node = graph.get_node(edge.target)
                if cfg_node is not None and not subgraph.has_node(cfg_node.id):
                    subgraph.add_node(cfg_node.clone(), score=0.2)
                    subgraph.add_edge(edge)

    def fit_to_budget(self, subgraph: SubGraph, budget: int, seed_ids: set[str]) -> None:
        while subgraph.token_estimate > budget:
            candidate = self.find_least_important_leaf(subgraph, seed_ids)
            if candidate is None:
                break
            if candidate.raw_code and candidate.summary:
                candidate.raw_code = None
                if subgraph.token_estimate <= budget:
                    break
            subgraph.remove_node(candidate.id)

    @staticmethod
    def find_least_important_leaf(subgraph: SubGraph, seed_ids: set[str]) -> Optional[Node]:
        targets = {edge.target for edge in subgraph.edges}
        leaves = [
            node
            for node in subgraph.nodes.values()
            if node.id not in seed_ids and node.id not in targets
        ]
        if not leaves:
            leaves = [node for node in subgraph.nodes.values() if node.id not in seed_ids]
        if not leaves:
            return None
        return min(leaves, key=lambda node: subgraph.scores.get(node.id, 0.0))

    @staticmethod
    def topological_order(subgraph: SubGraph) -> list[Node]:
        nodes = list(subgraph.nodes.values())
        types: list[Node] = []
        configs: list[Node] = []
        leaves: list[Node] = []
        others: list[Node] = []
        callee_ids = {edge.target for edge in subgraph.edges}

        for node in nodes:
            if node.kind == NodeKind.TYPE:
                types.append(node)
            elif node.kind in (NodeKind.CONFIG, NodeKind.CONSTANT):
                configs.append(node)
            elif node.id not in callee_ids:
                leaves.append(node)
            else:
                others.append(node)

        key = lambda node: subgraph.scores.get(node.id, 0.0)
        types.sort(key=key, reverse=True)
        configs.sort(key=key, reverse=True)
        others.sort(key=key, reverse=True)
        leaves.sort(key=key, reverse=True)
        return types + configs + others + leaves

    def update_retrieval_metadata(
        self,
        nodes: list[Node],
        *,
        graph: Optional[CodeGraph] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        persisted_nodes: list[Node] = []
        seen_ids: set[str] = set()
        for node in nodes:
            target = graph.get_node(node.id) if graph is not None else node
            if target is None or target.id in seen_ids:
                continue
            target.retrieval_count += 1
            target.last_retrieved = now
            node.retrieval_count = target.retrieval_count
            node.last_retrieved = target.last_retrieved
            persisted_nodes.append(target)
            seen_ids.add(target.id)

        if self._temporal_service is not None and persisted_nodes:
            self._temporal_service.update_co_retrieval(persisted_nodes)
            for node in nodes:
                target = graph.get_node(node.id) if graph is not None else node
                if target is not None:
                    node.co_retrieved_with = dict(target.co_retrieved_with)

        if self._graph_store is not None and persisted_nodes:
            self._graph_store.update_retrieval_metadata(persisted_nodes)

    def apply_quality_adjustment(self, subgraph: SubGraph, graph: CodeGraph) -> None:
        if self._quality_service is None:
            return
        for node_id, node in subgraph.nodes.items():
            original = subgraph.scores.get(node_id, 0.5)
            quality = self._quality_service.compute_quality_score(node, graph)
            subgraph.scores[node_id] = 0.85 * original + 0.15 * quality

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def generate_summary(subgraph: SubGraph, query: str) -> str:
        repos = subgraph.repos_involved
        node_count = len(subgraph.nodes)
        return f"Context for: \"{query}\" — {node_count} nodes from {', '.join(repos) if repos else 'unknown'}"
