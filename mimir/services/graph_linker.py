"""Cross-file and cross-repo graph linking.

Extracted from IndexingService to isolate resolution, inheritance detection,
API contract matching, and shared import detection into a focused module.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from mimir.domain.graph import CodeGraph
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind

logger = logging.getLogger(__name__)


def resolve_cross_file_refs(graph: CodeGraph, parser: object) -> int:
    """Scan every symbol's code for references to other known symbols.

    Creates CALLS, USES_TYPE, and INHERITS edges across files.
    Returns the number of new edges created.
    """
    from mimir.domain.lang import detect_language

    if not hasattr(parser, "extract_identifiers"):
        logger.warning("Parser does not support extract_identifiers — skipping cross-file resolution")
        return 0

    # 1. Build name → [node] index (only symbols, not containers)
    name_index: dict[str, list[Node]] = {}
    for node in graph.all_nodes():
        if node.is_symbol:
            name_index.setdefault(node.name, []).append(node)

    ambiguous_names = {
        name for name, nodes in name_index.items() if len(nodes) > 20
    }

    # 2. For each symbol, extract identifiers and resolve
    edges_created = 0
    seen_edges: set[tuple[str, str, str]] = set()

    for edge in graph.all_edges():
        if edge.kind != EdgeKind.CONTAINS:
            seen_edges.add((edge.source, edge.target, edge.kind.value))

    for node in graph.all_nodes():
        if not node.is_symbol or not node.raw_code:
            continue

        lang = detect_language(node.path) if node.path else None
        identifiers = parser.extract_identifiers(
            node.raw_code, language=lang, file_path=node.path,
        )

        inherits_names = detect_inheritance(node)

        for ident in identifiers:
            if ident == node.name or ident in ambiguous_names:
                continue

            targets = name_index.get(ident)
            if not targets:
                continue

            for target in targets:
                if target.id == node.id:
                    continue

                if ident in inherits_names:
                    edge_kind = EdgeKind.INHERITS
                elif target.kind in (NodeKind.CLASS, NodeKind.TYPE):
                    edge_kind = EdgeKind.USES_TYPE
                else:
                    edge_kind = EdgeKind.CALLS

                edge_key = (node.id, target.id, edge_kind.value)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)

                graph.add_edge(Edge(source=node.id, target=target.id, kind=edge_kind))
                edges_created += 1

    logger.info("Cross-file resolution: %d new edges created", edges_created)
    return edges_created


def detect_inheritance(node: Node) -> set[str]:
    """Extract parent type names from a class/struct/enum signature.

    Language-agnostic: handles Python (parenthesised), Swift/Kotlin/C#
    (colon-separated), and Java/JS/TS (extends/implements).
    """
    if node.kind not in (NodeKind.CLASS, NodeKind.TYPE):
        return set()

    sig = node.signature or ""
    if not sig:
        if node.raw_code:
            sig = node.raw_code.split("\n", 1)[0]
        else:
            return set()

    names: set[str] = set()

    # Pattern 1: parenthesised bases — class Foo(Bar, Baz):
    m = re.search(r"\(\s*([^)]+)\)", sig)
    if m:
        for part in m.group(1).split(","):
            base = re.split(r"[<\[\(=]", part.strip())[0].strip()
            if base and re.match(r"^[A-Z]\w*$", base):
                names.add(base)

    # Pattern 2: colon-separated — class Foo : Bar, Baz
    m = re.search(r"(?:class|struct|enum|protocol|interface)\s+\w+\s*:\s*(.+?)(?:\{|where|$)", sig)
    if m:
        for part in m.group(1).split(","):
            base = re.split(r"[<\[\(]", part.strip())[0].strip()
            if base and re.match(r"^[A-Z]\w*$", base):
                names.add(base)

    # Pattern 3: extends / implements keywords
    for kw in ("extends", "implements"):
        m = re.search(rf"{kw}\s+([\w,\s<>]+?)(?:\{{|implements|$)", sig)
        if m:
            for part in m.group(1).split(","):
                base = re.split(r"[<\[\(]", part.strip())[0].strip()
                if base and re.match(r"^[A-Z]\w*$", base):
                    names.add(base)

    return names


def normalize_route(url: str) -> str:
    """Normalize a URL to its path for route matching.

    Strips scheme+host, trailing slash, lowercases, and collapses path
    parameter placeholders and numeric IDs to ``{_}``.
    """
    parsed = urlparse(url)
    path = parsed.path or url
    path = path.rstrip("/") or "/"
    path = re.sub(r"\{[^}]+\}", "{_}", path)
    path = re.sub(r"/:([a-zA-Z_]\w*)", "/{_}", path)
    path = re.sub(r"/\d+", "/{_}", path)
    return path.lower()


def detect_api_contracts(graph: CodeGraph) -> None:
    """Detect cross-repo API call relationships using normalized route matching."""
    endpoints: dict[str, str] = {}
    for node in graph.all_nodes():
        if node.kind == NodeKind.API_ENDPOINT and node.route_path:
            norm = normalize_route(node.route_path)
            endpoints[norm] = node.id

    if not endpoints:
        return

    url_call_patterns = [
        re.compile(
            r'(?:requests|httpx|aiohttp)\.(get|post|put|delete|patch)\s*\([^)]*["\']([^"\']*)',
            re.IGNORECASE,
        ),
        re.compile(
            r'fetch\s*\(\s*[`"\']([^`"\']+)',
            re.IGNORECASE,
        ),
    ]
    for node in graph.symbol_nodes():
        if not node.raw_code:
            continue
        for pattern in url_call_patterns:
            for match in pattern.finditer(node.raw_code):
                url = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
                client_norm = normalize_route(url)

                ep_id = endpoints.get(client_norm)

                if ep_id is None:
                    for ep_path, candidate_id in endpoints.items():
                        if client_norm.endswith(ep_path) and (
                            len(client_norm) == len(ep_path)
                            or client_norm[-len(ep_path) - 1] == "/"
                        ):
                            ep_id = candidate_id
                            break

                if ep_id is None:
                    continue
                ep_node = graph.get_node(ep_id)
                if ep_node and ep_node.repo != node.repo:
                    graph.add_edge(Edge(
                        source=node.id,
                        target=ep_id,
                        kind=EdgeKind.API_CALLS,
                        metadata={"url": url},
                    ))
                    logger.info("Cross-repo API call: %s → %s (%s)", node.id, ep_id, url)


def detect_shared_imports(graph: CodeGraph) -> None:
    """Detect shared library usage across repos."""
    for edge in list(graph.all_edges()):
        if edge.kind == EdgeKind.IMPORTS:
            src = graph.get_node(edge.source)
            tgt = graph.get_node(edge.target)
            if src and tgt and src.repo != tgt.repo:
                graph.add_edge(Edge(
                    source=edge.source,
                    target=edge.target,
                    kind=EdgeKind.SHARED_LIB,
                ))
