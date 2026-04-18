"""Search and matching helpers for retrieval."""

from __future__ import annotations

import re
from typing import Optional

from mimir.domain.config import MimirConfig
from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind
from mimir.ports.vector_store import VectorStore


class RetrievalMatchingOps:
    """Owns vector, name, and route seed-selection logic."""

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
    _ROUTE_PATTERN = re.compile(
        r"""
        (?:(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+)?
        (?P<path>/[^\s]*)
        """,
        re.VERBOSE | re.IGNORECASE,
    )

    def __init__(self, config: MimirConfig, vector_store: VectorStore | None) -> None:
        self._config = config
        self._vector_store = vector_store

    def flat_search(
        self,
        query_embedding: list[float],
        graph: CodeGraph,
        *,
        top_k: int = 20,
        where: Optional[dict] = None,
    ) -> list[tuple[Node, float]]:
        if self._vector_store is None:
            raise RuntimeError("Vector store is required for flat_search")
        results = self._vector_store.search(query_embedding, top_k=top_k, where=where)
        return [
            (node, result.score)
            for result in results
            if (node := graph.get_node(result.id)) is not None
        ]

    def hierarchical_beam_search(
        self,
        query_embedding: list[float],
        graph: CodeGraph,
        beam_width: int,
        *,
        where: Optional[dict] = None,
    ) -> list[tuple[Node, float]]:
        if self._vector_store is None:
            raise RuntimeError("Vector store is required for hierarchical_beam_search")
        candidates: list[tuple[Node, float]] = []

        for level_kind in (NodeKind.REPOSITORY, NodeKind.MODULE, NodeKind.FILE):
            level_filter = {"kind": level_kind.value} if not where else {**where, "kind": level_kind.value}
            level_results = self._vector_store.search(
                query_embedding,
                top_k=beam_width,
                where=level_filter,
            )
            for result in level_results[:beam_width]:
                node = graph.get_node(result.id)
                if node is not None:
                    candidates.append((node, result.score))

        symbol_results = self._vector_store.search(
            query_embedding,
            top_k=beam_width * 3,
            where=where,
        )
        for result in symbol_results:
            node = graph.get_node(result.id)
            if node is not None and node.is_symbol:
                candidates.append((node, result.score))

        seen: set[str] = set()
        unique: list[tuple[Node, float]] = []
        for node, score in sorted(candidates, key=lambda item: item[1], reverse=True):
            if node.id in seen:
                continue
            seen.add(node.id)
            unique.append((node, score))
        return unique[: beam_width * 3]

    @classmethod
    def split_name(cls, name: str) -> set[str]:
        parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
        tokens = re.split(r"[^a-zA-Z0-9]+", parts)
        return {token.lower() for token in tokens if len(token) > 1}

    def name_match_seeds(
        self,
        query: str,
        graph: CodeGraph,
        repos: Optional[list[str]] = None,
    ) -> list[tuple[Node, float]]:
        query_words = set()
        for word in query.split():
            query_words |= self.split_name(word)
        query_words = {word for word in query_words if len(word) > 2 and word not in self._STOPWORDS}
        if not query_words:
            return []

        boost = self._config.retrieval.hybrid_alpha
        matches: list[tuple[Node, float]] = []
        for node in graph.all_nodes():
            if repos and node.repo not in repos:
                continue

            overlap = query_words & self.split_name(node.name)
            if overlap:
                ratio = len(overlap) / len(query_words)
                matches.append((node, boost * (0.5 + 0.5 * ratio)))
                continue

            if node.path:
                path_lower = node.path.lower()
                if any(word in path_lower for word in query_words):
                    matches.append((node, boost * 0.4))

        matches.sort(key=lambda item: item[1], reverse=True)
        return matches[:30]

    @classmethod
    def route_match_seeds(
        cls,
        query: str,
        graph: CodeGraph,
        repos: Optional[list[str]] = None,
    ) -> list[tuple[Node, float]]:
        match = cls._ROUTE_PATTERN.search(query)
        if not match:
            return []

        query_method = (match.group("method") or "").upper()
        query_path = match.group("path").rstrip("/").lower() or "/"
        query_path_collapsed = re.sub(r"\{[^}]+\}", "{_}", query_path)

        matches: list[tuple[Node, float]] = []
        for node in graph.all_nodes():
            if node.kind != NodeKind.API_ENDPOINT or not node.route_path:
                continue
            if repos and node.repo not in repos:
                continue

            node_path = node.route_path.rstrip("/").lower() or "/"
            node_path_collapsed = re.sub(r"\{[^}]+\}", "{_}", node_path)
            if node_path_collapsed != query_path_collapsed:
                if not (
                    node_path_collapsed.endswith(query_path_collapsed)
                    and (
                        len(node_path_collapsed) == len(query_path_collapsed)
                        or node_path_collapsed[-len(query_path_collapsed) - 1] == "/"
                    )
                ):
                    continue

            score = 1.0
            if query_method and node.http_method:
                score = 1.2 if query_method == node.http_method.upper() else 0.6
            matches.append((node, score))

        matches.sort(key=lambda item: item[1], reverse=True)
        return matches
