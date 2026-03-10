"""Retrieval service — context assembly engine.

Implements the core innovation: query → seed → subgraph expansion →
temporal reranking → budget fitting → topological ordering → ContextBundle.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from typing import Optional

from mimir.domain.config import MimirConfig
from mimir.domain.graph import CodeGraph
from mimir.domain.models import SYMBOL_KINDS, Edge, EdgeKind, Node, NodeKind, EDGE_EXPANSION_WEIGHTS
from mimir.domain.subgraph import ContextBundle, SubGraph
from mimir.ports.embedder import Embedder
from mimir.ports.vector_store import VectorStore

logger = logging.getLogger(__name__)


class RetrievalService:
    """Assembles minimal, connected, ordered context subgraphs."""

    def __init__(
        self,
        config: MimirConfig,
        embedder: Embedder,
        vector_store: VectorStore,
    ) -> None:
        self._config = config
        self._embedder = embedder
        self._vector_store = vector_store

    async def search(
        self,
        query: str,
        graph: CodeGraph,
        *,
        token_budget: Optional[int] = None,
        beam_width: Optional[int] = None,
        repos: Optional[list[str]] = None,
        flat: bool = False,
    ) -> ContextBundle:
        """Primary search: assemble a context subgraph for a query.

        Parameters
        ----------
        query
            Natural language query.
        graph
            The full code graph.
        token_budget
            Max tokens in the result. Defaults to config value.
        beam_width
            Width for hierarchical beam search.
        repos
            Optional repo filter.
        flat
            Force flat search even if summaries exist.
        """
        budget = token_budget or self._config.retrieval.default_token_budget
        width = beam_width or self._config.retrieval.default_beam_width
        mode = self._config.indexing.summary_mode

        # Step 1: Embed the query
        embeddings = await self._embedder.embed_batch([query])
        query_embedding = embeddings[0]

        # Step 2: Find seed nodes
        where_filter = {"repo": repos[0]} if repos and len(repos) == 1 else None
        if mode == "none" or flat:
            seeds = self._flat_search(query_embedding, graph, where=where_filter)
        else:
            seeds = self._hierarchical_beam_search(
                query_embedding, graph, width, where=where_filter,
            )

        if not seeds:
            return ContextBundle(
                nodes=[], edges=[], summary="No results found.",
                token_count=0, repos_involved=[],
            )

        # Filter by repo if multi-repo filter provided
        if repos and len(repos) > 1:
            seeds = [s for s in seeds if s[0].repo in repos]

        # Step 2b: BM25 keyword search (hybrid)
        alpha = self._config.retrieval.hybrid_alpha
        bm25_results = self._bm25_search(query, graph, top_k=width * 3, repos=repos)
        if bm25_results:
            seed_ids_so_far = {s[0].id for s in seeds}
            for node, bm25_score in bm25_results:
                # Blend: alpha * vector + (1 - alpha) * bm25
                blended = (1.0 - alpha) * bm25_score
                if node.id in seed_ids_so_far:
                    seeds = [
                        (n, alpha * s + blended) if n.id == node.id else (n, s)
                        for n, s in seeds
                    ]
                else:
                    seeds.append((node, blended))
                    seed_ids_so_far.add(node.id)
            seeds.sort(key=lambda x: x[1], reverse=True)

        # Step 2c: Name-based keyword matching
        name_matches = self._name_match_seeds(query, graph, repos)
        if name_matches:
            seed_ids_so_far = {s[0].id for s in seeds}
            for node, score in name_matches:
                if node.id in seed_ids_so_far:
                    # Boost existing seed score
                    seeds = [
                        (n, max(s, score)) if n.id == node.id else (n, s)
                        for n, s in seeds
                    ]
                else:
                    seeds.append((node, score))
                    seed_ids_so_far.add(node.id)
            # Re-sort by score
            seeds.sort(key=lambda x: x[1], reverse=True)

        # Step 3: Build subgraph
        seed_nodes = [s[0] for s in seeds]
        seed_scores = {s[0].id: s[1] for s in seeds}
        subgraph = self._expand_subgraph(
            seed_nodes, seed_scores, query_embedding, graph,
        )

        # Step 4: Add type and config context
        self._add_type_context(subgraph, graph)
        self._add_config_context(subgraph, graph)

        # Step 5: Fit to budget
        self._fit_to_budget(subgraph, budget, seed_ids={n.id for n in seed_nodes})

        # Step 6: Topological ordering
        ordered = self._topological_order(subgraph)

        # Step 7: Build final bundle
        return ContextBundle(
            nodes=ordered,
            edges=subgraph.edges,
            summary=self._generate_summary(subgraph, query),
            token_count=sum(n.token_estimate for n in ordered),
            repos_involved=subgraph.repos_involved,
            seed_ids=[n.id for n in seed_nodes],
        )

    # ------------------------------------------------------------------
    # BM25 index (built lazily on first search)
    # ------------------------------------------------------------------

    _bm25_index = None
    _bm25_node_ids: list[str] = []

    def _ensure_bm25(self, graph: CodeGraph) -> None:
        """Build a BM25 index over all graph nodes (lazy, once per session)."""
        if self._bm25_index is not None:
            return
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            logger.debug("rank-bm25 not installed, skipping BM25 hybrid")
            return

        corpus: list[list[str]] = []
        node_ids: list[str] = []
        for node in graph.all_nodes():
            text = node.raw_code or node.summary or node.name
            tokens = re.findall(r"[a-zA-Z_]\w{2,}", text)
            # Also add camelCase-split tokens
            expanded: list[str] = []
            for t in tokens:
                expanded.append(t.lower())
                expanded.extend(self._split_name(t))
            corpus.append(expanded)
            node_ids.append(node.id)

        if corpus:
            self._bm25_index = BM25Okapi(corpus)
            self._bm25_node_ids = node_ids
            logger.info("BM25 index built: %d documents", len(corpus))

    def _bm25_search(
        self,
        query: str,
        graph: CodeGraph,
        top_k: int = 20,
        repos: Optional[list[str]] = None,
    ) -> list[tuple[Node, float]]:
        """Keyword search via BM25."""
        self._ensure_bm25(graph)
        if self._bm25_index is None:
            return []

        tokens = re.findall(r"[a-zA-Z_]\w{2,}", query)
        expanded: list[str] = []
        for t in tokens:
            expanded.append(t.lower())
            expanded.extend(self._split_name(t))

        if not expanded:
            return []

        scores = self._bm25_index.get_scores(expanded)
        # Normalise scores to [0, 1]
        max_score = max(scores) if len(scores) > 0 else 1.0
        if max_score <= 0:
            return []

        ranked = sorted(
            zip(self._bm25_node_ids, scores),
            key=lambda x: x[1],
            reverse=True,
        )

        results: list[tuple[Node, float]] = []
        for node_id, score in ranked[:top_k]:
            if score <= 0:
                break
            node = graph.get_node(node_id)
            if node and (not repos or node.repo in repos):
                results.append((node, score / max_score))

        return results

    # ------------------------------------------------------------------
    # Search strategies
    # ------------------------------------------------------------------

    def _flat_search(
        self,
        query_embedding: list[float],
        graph: CodeGraph,
        top_k: int = 20,
        where: Optional[dict] = None,
    ) -> list[tuple[Node, float]]:
        """Flat vector search (none mode)."""
        results = self._vector_store.search(query_embedding, top_k=top_k, where=where)
        out: list[tuple[Node, float]] = []
        for r in results:
            node = graph.get_node(r.id)
            if node:
                out.append((node, r.score))
        return out

    def _hierarchical_beam_search(
        self,
        query_embedding: list[float],
        graph: CodeGraph,
        beam_width: int,
        where: Optional[dict] = None,
    ) -> list[tuple[Node, float]]:
        """Top-down beam search: repo → module → file → symbol."""
        candidates: list[tuple[Node, float]] = []

        # Search at each hierarchy level, keeping top-k at each stage
        for level_kind in [NodeKind.REPOSITORY, NodeKind.MODULE, NodeKind.FILE]:
            level_results = self._vector_store.search(
                query_embedding,
                top_k=beam_width,
                where={"kind": level_kind.value} if not where else {**(where or {}), "kind": level_kind.value},
            )
            # Keep beam_width candidates for next level
            for r in level_results[:beam_width]:
                node = graph.get_node(r.id)
                if node:
                    candidates.append((node, r.score))

        # Final search at symbol level
        symbol_results = self._vector_store.search(
            query_embedding,
            top_k=beam_width * 3,
            where=where,
        )
        for r in symbol_results:
            node = graph.get_node(r.id)
            if node and node.is_symbol:
                candidates.append((node, r.score))

        # Sort by score, deduplicate
        seen: set[str] = set()
        unique: list[tuple[Node, float]] = []
        for node, score in sorted(candidates, key=lambda x: x[1], reverse=True):
            if node.id not in seen:
                seen.add(node.id)
                unique.append((node, score))

        return unique[:beam_width * 3]

    # ------------------------------------------------------------------
    # Name matching
    # ------------------------------------------------------------------

    _STOPWORDS = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "do", "does", "did", "has", "have", "had", "in", "on", "at",
        "to", "for", "of", "with", "by", "from", "and", "or", "not",
        "it", "its", "this", "that", "how", "what", "where", "when",
        "why", "who", "which", "all", "each", "every", "any", "my",
        "your", "our", "use", "get", "set", "can", "will", "should",
        "about", "into", "out", "up", "down", "code", "function",
        "class", "method", "file", "does", "work", "show", "find",
    })

    @staticmethod
    def _split_name(name: str) -> set[str]:
        """Split a symbol name into lowercase words.

        Handles camelCase, PascalCase, snake_case, and kebab-case.
        e.g. "HomeView" → {"home", "view"}
             "get_user_name" → {"get", "user", "name"}
        """
        # Insert boundary before uppercase runs: "HomeView" → "Home View"
        parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        # Split acronym runs: "XMLParser" → "XML Parser"
        parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
        # Split on non-alphanumeric
        tokens = re.split(r"[^a-zA-Z0-9]+", parts)
        return {t.lower() for t in tokens if len(t) > 1}

    def _name_match_seeds(
        self,
        query: str,
        graph: CodeGraph,
        repos: Optional[list[str]] = None,
    ) -> list[tuple[Node, float]]:
        """Find nodes whose name or path matches words in the query."""
        # Extract significant words from query
        query_words = set()
        for w in query.split():
            # Also split query words by camelCase in case user writes "HomeView"
            query_words |= self._split_name(w)
        query_words = {w for w in query_words if len(w) > 2 and w not in self._STOPWORDS}

        if not query_words:
            return []

        boost = self._config.retrieval.hybrid_alpha
        matches: list[tuple[Node, float]] = []

        for node in graph.all_nodes():
            if repos and node.repo not in repos:
                continue

            # Split node name into words for matching
            name_words = self._split_name(node.name)
            overlap = query_words & name_words

            if overlap:
                # Score based on fraction of query words matched
                ratio = len(overlap) / len(query_words)
                score = boost * (0.5 + 0.5 * ratio)  # range: 0.5*boost → boost
                matches.append((node, score))
                continue

            # Path matching: check if query words appear in the file path
            if node.path:
                path_lower = node.path.lower()
                for word in query_words:
                    if word in path_lower:
                        matches.append((node, boost * 0.4))
                        break

        # Sort by score desc, cap results
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[:30]

    # ------------------------------------------------------------------
    # Subgraph expansion
    # ------------------------------------------------------------------

    def _expand_subgraph(
        self,
        seeds: list[Node],
        seed_scores: dict[str, float],
        query_embedding: list[float],
        graph: CodeGraph,
    ) -> SubGraph:
        """BFS expansion from seeds, following dependency edges."""
        hops = self._config.retrieval.expansion_hops
        gate = self._config.retrieval.relevance_gate

        subgraph = SubGraph()
        for seed in seeds:
            subgraph.add_node(seed, score=seed_scores.get(seed.id, 1.0))

        frontier = {s.id for s in seeds}
        visited: set[str] = set()

        for hop in range(hops):
            next_frontier: set[str] = set()
            for node_id in frontier:
                if node_id in visited:
                    continue
                visited.add(node_id)

                for edge in graph.get_all_edges_for(node_id):
                    target_id = edge.target if edge.source == node_id else edge.source
                    if target_id in visited or target_id in subgraph.node_ids:
                        # Still add the edge for connectivity
                        if subgraph.has_node(target_id):
                            subgraph.add_edge(edge)
                        continue

                    target = graph.get_node(target_id)
                    if target is None:
                        continue

                    # Relevance gate: check if target is related to query
                    if target.embedding and query_embedding:
                        sim = self._cosine_similarity(query_embedding, target.embedding)
                        if sim < gate:
                            continue
                    else:
                        sim = 0.5  # unknown, moderate weight

                    edge_weight = EDGE_EXPANSION_WEIGHTS.get(edge.kind, 0.5)
                    score = sim * edge_weight

                    subgraph.add_node(target, score=score)
                    subgraph.add_edge(edge)
                    next_frontier.add(target_id)

            frontier = next_frontier

        return subgraph

    def _add_type_context(self, subgraph: SubGraph, graph: CodeGraph) -> None:
        """Add type definitions used by nodes in the subgraph."""
        for node in list(subgraph.nodes.values()):
            type_edges = graph.get_outgoing_edges(node.id, EdgeKind.USES_TYPE)
            for edge in type_edges:
                type_node = graph.get_node(edge.target)
                if type_node and not subgraph.has_node(type_node.id):
                    subgraph.add_node(type_node, score=0.3)
                    subgraph.add_edge(edge)

    def _add_config_context(self, subgraph: SubGraph, graph: CodeGraph) -> None:
        """Add config nodes read by nodes in the subgraph."""
        for node in list(subgraph.nodes.values()):
            config_edges = graph.get_outgoing_edges(node.id, EdgeKind.READS_CONFIG)
            for edge in config_edges:
                cfg_node = graph.get_node(edge.target)
                if cfg_node and not subgraph.has_node(cfg_node.id):
                    subgraph.add_node(cfg_node, score=0.2)
                    subgraph.add_edge(edge)

    # ------------------------------------------------------------------
    # Budget fitting
    # ------------------------------------------------------------------

    def _fit_to_budget(
        self,
        subgraph: SubGraph,
        budget: int,
        seed_ids: set[str],
    ) -> None:
        """Prune least important nodes until subgraph fits in token budget."""
        while subgraph.token_estimate > budget:
            # Find least important non-seed leaf
            candidate = self._find_least_important_leaf(subgraph, seed_ids)
            if candidate is None:
                break

            # First try: replace code with summary
            if candidate.raw_code and candidate.summary:
                candidate.raw_code = None  # keep summary only
                if subgraph.token_estimate <= budget:
                    break

            # Still over: remove entirely
            subgraph.remove_node(candidate.id)

    @staticmethod
    def _find_least_important_leaf(
        subgraph: SubGraph,
        seed_ids: set[str],
    ) -> Optional[Node]:
        """Find the least important non-seed leaf node."""
        targets = {e.target for e in subgraph.edges}
        leaves = [
            n for n in subgraph.nodes.values()
            if n.id not in seed_ids and n.id not in targets
        ]
        if not leaves:
            # Try non-seed nodes
            leaves = [n for n in subgraph.nodes.values() if n.id not in seed_ids]
        if not leaves:
            return None
        return min(leaves, key=lambda n: subgraph.scores.get(n.id, 0.0))

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------

    @staticmethod
    def _topological_order(subgraph: SubGraph) -> list[Node]:
        """Order nodes for LLM comprehension: types → configs → leaves → callers."""
        nodes = list(subgraph.nodes.values())

        # Group by category
        types: list[Node] = []
        configs: list[Node] = []
        leaves: list[Node] = []
        others: list[Node] = []

        callee_ids = {e.target for e in subgraph.edges}

        for node in nodes:
            if node.kind == NodeKind.TYPE:
                types.append(node)
            elif node.kind in (NodeKind.CONFIG, NodeKind.CONSTANT):
                configs.append(node)
            elif node.id not in callee_ids:
                leaves.append(node)
            else:
                others.append(node)

        # Sort each group by score (most relevant first)
        key = lambda n: subgraph.scores.get(n.id, 0.0)
        types.sort(key=key, reverse=True)
        configs.sort(key=key, reverse=True)
        others.sort(key=key, reverse=True)
        leaves.sort(key=key, reverse=True)

        return types + configs + others + leaves

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _generate_summary(subgraph: SubGraph, query: str) -> str:
        repos = subgraph.repos_involved
        node_count = len(subgraph.nodes)
        return (
            f"Context for: \"{query}\" — {node_count} nodes "
            f"from {', '.join(repos) if repos else 'unknown'}"
        )


