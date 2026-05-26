"""Retrieval service — context assembly engine.

Implements the core innovation: query → seed → subgraph expansion →
temporal reranking → budget fitting → topological ordering → ContextBundle.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from mimir.domain.config import MimirConfig
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
from mimir.domain.subgraph import ContextBundle, SubGraph
from mimir.ports.embedder import Embedder
from mimir.ports.vector_store import VectorStore
from mimir.services.intent import classify_intent, INTENT_PROFILES
from mimir.services.retrieval.graph_ops import RetrievalGraphOps
from mimir.services.retrieval.matching import RetrievalMatchingOps

from mimir.ports.graph_store import GraphStore

if TYPE_CHECKING:
    from mimir.services.quality import QualityService
    from mimir.services.temporal import TemporalService

logger = logging.getLogger(__name__)


class RetrievalService:
    """Assembles minimal, connected, ordered context subgraphs."""

    def __init__(
        self,
        config: MimirConfig,
        embedder: Embedder,
        vector_store: VectorStore,
        quality_service: Optional[QualityService] = None,
        temporal_service: Optional[TemporalService] = None,
        graph_store: Optional[GraphStore] = None,
    ) -> None:
        self._config = config
        self._embedder = embedder
        self._vector_store = vector_store
        self._quality_service = quality_service
        self._temporal_service = temporal_service
        self._graph_store = graph_store
        self._matching_ops = RetrievalMatchingOps(config, vector_store)
        self._graph_ops = RetrievalGraphOps(
            config,
            quality_service=quality_service,
            temporal_service=temporal_service,
            graph_store=graph_store,
        )

    def _matching_component(self) -> RetrievalMatchingOps:
        component = getattr(self, "_matching_ops", None)
        if component is None:
            component = RetrievalMatchingOps(self._config, getattr(self, "_vector_store", None))
            self._matching_ops = component
        return component

    def _graph_component(self) -> RetrievalGraphOps:
        component = getattr(self, "_graph_ops", None)
        if component is None:
            component = RetrievalGraphOps(
                self._config,
                quality_service=getattr(self, "_quality_service", None),
                temporal_service=getattr(self, "_temporal_service", None),
                graph_store=getattr(self, "_graph_store", None),
            )
            self._graph_ops = component
        return component

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
            Force flat vector search instead of hierarchical beam search.
        """
        budget = token_budget or self._config.retrieval.default_token_budget
        width = beam_width or self._config.retrieval.default_beam_width

        # Step 0: Classify query intent and apply parameter overrides
        intent = classify_intent(query)
        profile = INTENT_PROFILES[intent]
        alpha = profile.hybrid_alpha
        hops = profile.expansion_hops
        gate = profile.relevance_gate
        logger.debug("Query intent: %s (alpha=%.2f, hops=%d, gate=%.2f)", intent.value, alpha, hops, gate)

        # Step 1: Embed the query
        embeddings = await self._embedder.embed_batch([query])
        query_embedding = embeddings[0]

        # Step 2: Find seed nodes
        where_filter = {"repo": repos[0]} if repos and len(repos) == 1 else None
        if flat:
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

        # Step 2d: Route-based matching for API endpoints
        route_matches = self._route_match_seeds(query, graph, repos)
        if route_matches:
            seed_ids_so_far = {s[0].id for s in seeds}
            for node, score in route_matches:
                if node.id in seed_ids_so_far:
                    # Boost: route match is high-confidence
                    seeds = [
                        (n, max(s, score)) if n.id == node.id else (n, s)
                        for n, s in seeds
                    ]
                else:
                    seeds.append((node, score))
                    seed_ids_so_far.add(node.id)
            seeds.sort(key=lambda x: x[1], reverse=True)

        # Step 2e: Trim seeds to fit within token budget
        # Seeds are sorted by score (descending). Keep only the top seeds
        # whose cumulative token cost stays under the budget, reserving
        # room for expansion context.
        seed_budget = int(budget * 0.75)  # reserve 25% for expanded neighbours
        trimmed_seeds: list[tuple[Node, float]] = []
        running_tokens = 0
        for node, score in seeds:
            cost = node.token_estimate
            if running_tokens + cost > seed_budget and trimmed_seeds:
                break
            trimmed_seeds.append((node, score))
            running_tokens += cost
        seeds = trimmed_seeds

        # Step 3: Build subgraph
        seed_nodes = [s[0] for s in seeds]
        seed_scores = {s[0].id: s[1] for s in seeds}
        subgraph = self._expand_subgraph(
            seed_nodes, seed_scores, query_embedding, graph,
            hops=hops, gate=gate,
        )

        # Step 4: Add type and config context
        self._add_type_context(subgraph, graph)
        self._add_config_context(subgraph, graph)

        # Step 4b: Apply quality score adjustment
        if self._quality_service is not None:
            self._apply_quality_adjustment(subgraph, graph)

        # Step 5: Fit to budget
        self._fit_to_budget(subgraph, budget, seed_ids={n.id for n in seed_nodes})

        # Step 6: Update retrieval metadata on retrieved nodes
        self._update_retrieval_metadata(list(subgraph.nodes.values()), graph=graph)

        # Step 7: Topological ordering
        ordered = self._topological_order(subgraph)

        # Step 8: Build final bundle
        return ContextBundle(
            nodes=ordered,
            edges=subgraph.edges,
            summary=self._generate_summary(subgraph, query),
            token_count=sum(n.token_estimate for n in ordered),
            repos_involved=subgraph.repos_involved,
            seed_ids=[n.id for n in seed_nodes],
            query_embedding=query_embedding,
        )

    # ------------------------------------------------------------------
    # BM25 index (built lazily on first search)
    # ------------------------------------------------------------------

    _bm25_index = None
    _bm25_node_ids: list[str] = []

    @property
    def default_token_budget(self) -> int:
        """Configured default token budget used when none is supplied."""
        return self._config.retrieval.default_token_budget

    def invalidate_bm25(self) -> None:
        """Reset the cached BM25 index, forcing rebuild on next search."""
        self._bm25_index = None
        self._bm25_node_ids = []

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
        return self._matching_component().flat_search(
            query_embedding,
            graph,
            top_k=top_k,
            where=where,
        )

    def _hierarchical_beam_search(
        self,
        query_embedding: list[float],
        graph: CodeGraph,
        beam_width: int,
        where: Optional[dict] = None,
    ) -> list[tuple[Node, float]]:
        return self._matching_component().hierarchical_beam_search(
            query_embedding,
            graph,
            beam_width,
            where=where,
        )

    # ------------------------------------------------------------------
    # Name matching
    # ------------------------------------------------------------------

    @staticmethod
    def _split_name(name: str) -> set[str]:
        return RetrievalMatchingOps.split_name(name)

    def _name_match_seeds(
        self,
        query: str,
        graph: CodeGraph,
        repos: Optional[list[str]] = None,
    ) -> list[tuple[Node, float]]:
        return self._matching_component().name_match_seeds(query, graph, repos)

    # ------------------------------------------------------------------
    # Route matching
    # ------------------------------------------------------------------

    def _route_match_seeds(
        self,
        query: str,
        graph: CodeGraph,
        repos: Optional[list[str]] = None,
    ) -> list[tuple[Node, float]]:
        return self._matching_component().route_match_seeds(query, graph, repos)

    # ------------------------------------------------------------------
    # Subgraph expansion
    # ------------------------------------------------------------------

    def _expand_subgraph(
        self,
        seeds: list[Node],
        seed_scores: dict[str, float],
        query_embedding: list[float],
        graph: CodeGraph,
        *,
        hops: Optional[int] = None,
        gate: Optional[float] = None,
    ) -> SubGraph:
        return self._graph_component().expand_subgraph(
            seeds,
            seed_scores,
            query_embedding,
            graph,
            hops=hops,
            gate=gate,
        )

    def _add_type_context(self, subgraph: SubGraph, graph: CodeGraph) -> None:
        self._graph_component().add_type_context(subgraph, graph)

    def _add_config_context(self, subgraph: SubGraph, graph: CodeGraph) -> None:
        self._graph_component().add_config_context(subgraph, graph)

    # ------------------------------------------------------------------
    # Budget fitting
    # ------------------------------------------------------------------

    def _fit_to_budget(
        self,
        subgraph: SubGraph,
        budget: int,
        seed_ids: set[str],
    ) -> None:
        self._graph_component().fit_to_budget(subgraph, budget, seed_ids)

    def fit_subgraph_to_budget(
        self,
        subgraph: SubGraph,
        budget: int,
        *,
        seed_ids: Optional[set[str]] = None,
    ) -> None:
        """Public budget-fit wrapper used by transport adapters."""
        self._fit_to_budget(subgraph, budget, seed_ids=seed_ids or set())

    @staticmethod
    def _find_least_important_leaf(
        subgraph: SubGraph,
        seed_ids: set[str],
    ) -> Optional[Node]:
        return RetrievalGraphOps.find_least_important_leaf(subgraph, seed_ids)

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------

    @staticmethod
    def _topological_order(subgraph: SubGraph) -> list[Node]:
        return RetrievalGraphOps.topological_order(subgraph)

    # ------------------------------------------------------------------
    # Retrieval metadata
    # ------------------------------------------------------------------

    def _update_retrieval_metadata(
        self,
        nodes: list[Node],
        *,
        graph: Optional[CodeGraph] = None,
    ) -> None:
        self._graph_component().update_retrieval_metadata(nodes, graph=graph)

    # ------------------------------------------------------------------
    # Quality adjustment
    # ------------------------------------------------------------------

    def _apply_quality_adjustment(self, subgraph: SubGraph, graph: CodeGraph) -> None:
        self._graph_component().apply_quality_adjustment(subgraph, graph)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        return RetrievalGraphOps.cosine_similarity(a, b)

    @staticmethod
    def _generate_summary(subgraph: SubGraph, query: str) -> str:
        return RetrievalGraphOps.generate_summary(subgraph, query)
