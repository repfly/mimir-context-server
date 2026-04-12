"""Heuristic summarization for code graph nodes.

Extracted from IndexingService to isolate summary generation logic.
"""

from __future__ import annotations

from mimir.domain.graph import CodeGraph
from mimir.domain.models import Node, NodeKind


def generate_heuristic_summaries(graph: CodeGraph) -> None:
    """Generate summaries for every node in *graph*."""
    for node in graph.all_nodes():
        node.summary = heuristic_summary(node, graph)


def heuristic_summary(node: Node, graph: CodeGraph) -> str:
    """Build a structured summary without LLM."""
    parts: list[str] = []

    if node.kind in (NodeKind.FUNCTION, NodeKind.METHOD, NodeKind.API_ENDPOINT):
        if node.signature:
            parts.append(node.signature)
        if node.docstring:
            parts.append(node.docstring[:200])
        callees = graph.get_callees(node.id)
        if callees:
            parts.append(f"Calls: {', '.join(c.name for c in callees[:10])}")
        callers = graph.get_callers(node.id)
        if callers:
            parts.append(f"Called by: {', '.join(c.name for c in callers[:10])}")
        if node.http_method and node.route_path:
            parts.append(f"Route: {node.http_method} {node.route_path}")
    elif node.kind == NodeKind.FILE:
        children = graph.get_children(node.id)
        parts.append(f"File: {node.path}")
        for child in children[:20]:
            sig = child.signature or child.name
            doc = f" — {child.docstring[:80]}" if child.docstring else ""
            parts.append(f"  {sig}{doc}")
    elif node.kind == NodeKind.MODULE:
        children = graph.get_children(node.id)
        parts.append(f"Module: {node.name}")
        for child in children:
            symbol_count = len(graph.get_children(child.id))
            parts.append(f"  {child.name} ({symbol_count} symbols)")
    elif node.kind == NodeKind.REPOSITORY:
        modules = graph.get_children(node.id)
        parts.append(f"Repository: {node.name}")
        for mod in modules:
            file_count = len(graph.get_children(mod.id))
            parts.append(f"  {mod.name}/ ({file_count} files)")

    return "\n".join(parts) if parts else node.name
