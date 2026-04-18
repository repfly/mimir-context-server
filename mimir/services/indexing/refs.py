"""Cross-file reference resolution helpers for indexing flows."""

from __future__ import annotations

import logging

from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.services.graph_linker import detect_inheritance

logger = logging.getLogger(__name__)


def resolve_affected_refs(
    graph: CodeGraph,
    new_symbols: list[Node],
    parser: object,
) -> list[Edge]:
    """Resolve cross-file references for new/changed symbols only."""
    from mimir.domain.lang import detect_language

    name_index: dict[str, list[Node]] = {}
    for node in graph.all_nodes():
        if node.is_symbol:
            name_index.setdefault(node.name, []).append(node)

    ambiguous_names = {name for name, nodes in name_index.items() if len(nodes) > 20}

    new_edges: list[Edge] = []
    seen_edges: set[tuple[str, str, str]] = set()

    for edge in graph.all_edges():
        if edge.kind != EdgeKind.CONTAINS:
            seen_edges.add((edge.source, edge.target, edge.kind.value))

    new_symbol_ids = {node.id for node in new_symbols}
    new_symbol_names = {node.name for node in new_symbols}

    def try_add_edge(source_id: str, target: Node, ident: str, inherits_names: set[str]) -> None:
        if target.id == source_id:
            return
        if ident in inherits_names:
            edge_kind = EdgeKind.INHERITS
        elif target.kind in (NodeKind.CLASS, NodeKind.TYPE):
            edge_kind = EdgeKind.USES_TYPE
        else:
            edge_kind = EdgeKind.CALLS

        edge_key = (source_id, target.id, edge_kind.value)
        if edge_key in seen_edges:
            return
        seen_edges.add(edge_key)

        edge = Edge(source=source_id, target=target.id, kind=edge_kind)
        graph.add_edge(edge)
        new_edges.append(edge)

    for node in new_symbols:
        if not node.raw_code:
            continue
        lang = detect_language(node.path) if node.path else None
        identifiers = parser.extract_identifiers(node.raw_code, language=lang, file_path=node.path)
        inherits_names = detect_inheritance(node)

        for ident in identifiers:
            if ident == node.name or ident in ambiguous_names:
                continue
            targets = name_index.get(ident)
            if not targets:
                continue
            for target in targets:
                try_add_edge(node.id, target, ident, inherits_names)

    for node in graph.all_nodes():
        if not node.is_symbol or not node.raw_code:
            continue
        if node.id in new_symbol_ids:
            continue

        if not any(name in node.raw_code for name in new_symbol_names if name not in ambiguous_names):
            continue

        lang = detect_language(node.path) if node.path else None
        identifiers = parser.extract_identifiers(node.raw_code, language=lang, file_path=node.path)
        inherits_names = detect_inheritance(node)

        for ident in identifiers:
            if ident not in new_symbol_names or ident in ambiguous_names:
                continue
            targets = name_index.get(ident)
            if not targets:
                continue
            for target in targets:
                if target.id not in new_symbol_ids:
                    continue
                try_add_edge(node.id, target, ident, inherits_names)

    logger.info("Affected cross-file resolution: %d new edges", len(new_edges))
    return new_edges
