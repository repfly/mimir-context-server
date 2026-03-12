"""Write-path context — assemble what you need to know before editing a file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from mimir.domain.graph import CodeGraph
from mimir.domain.models import EdgeKind, Node, NodeKind
from mimir.services._conventions import find_test_file


@dataclass
class WriteContextBundle:
    """Everything an LLM needs to know before editing a file."""

    file_node: Optional[Node]
    symbols: list[Node]
    interfaces: list[Node]
    sibling_implementations: list[Node]
    test_file: Optional[Node]
    di_registrations: list[Node]
    imports_outgoing: list[Node]
    imports_incoming: list[Node]

    def format_for_llm(self) -> str:
        parts: list[str] = []

        if self.file_node:
            parts.append(f"## Write context for `{self.file_node.path}`\n")

        if self.symbols:
            parts.append("### Symbols in this file")
            for s in self.symbols:
                sig = s.signature or s.name
                parts.append(f"- `{sig}` ({s.kind.value})")
            parts.append("")

        if self.interfaces:
            parts.append("### Interfaces / base classes")
            for i in self.interfaces:
                code = i.raw_code or i.signature or i.name
                parts.append(f"```\n{code}\n```")
            parts.append("")

        if self.sibling_implementations:
            parts.append("### Sibling implementations")
            for s in self.sibling_implementations:
                sig = s.signature or s.name
                loc = f" ({s.path})" if s.path else ""
                parts.append(f"- `{sig}`{loc}")
            parts.append("")

        if self.test_file:
            parts.append(f"### Test file: `{self.test_file.path}`")
            if self.test_file.raw_code:
                parts.append(f"```\n{self.test_file.raw_code[:2000]}\n```")
            parts.append("")

        if self.di_registrations:
            parts.append("### DI / factory registrations")
            for d in self.di_registrations:
                sig = d.signature or d.name
                loc = f" ({d.path})" if d.path else ""
                parts.append(f"- `{sig}`{loc}")
            parts.append("")

        if self.imports_outgoing:
            parts.append("### This file imports")
            for i in self.imports_outgoing[:20]:
                parts.append(f"- `{i.name}` ({i.path or '?'})")
            parts.append("")

        if self.imports_incoming:
            parts.append("### Imported by")
            for i in self.imports_incoming[:20]:
                parts.append(f"- `{i.name}` ({i.path or '?'})")
            parts.append("")

        return "\n".join(parts) if parts else "No write context found."


class WriteContextService:
    """Assembles write-path context for a file."""

    def assemble(self, file_path: str, graph: CodeGraph) -> WriteContextBundle:
        """Build write context for the given file path."""
        file_node = self._find_file_node(file_path, graph)
        if not file_node:
            return WriteContextBundle(
                file_node=None, symbols=[], interfaces=[],
                sibling_implementations=[], test_file=None,
                di_registrations=[], imports_outgoing=[], imports_incoming=[],
            )

        # Symbols in the file
        symbols = graph.get_children(file_node.id)

        # Interfaces and sibling implementations
        interfaces: list[Node] = []
        siblings: list[Node] = []
        seen_interfaces: set[str] = set()

        for sym in symbols:
            if sym.kind not in (NodeKind.CLASS, NodeKind.TYPE):
                continue
            for edge_kind in (EdgeKind.INHERITS, EdgeKind.IMPLEMENTS):
                for edge in graph.get_outgoing_edges(sym.id, edge_kind):
                    iface = graph.get_node(edge.target)
                    if iface and iface.id not in seen_interfaces:
                        seen_interfaces.add(iface.id)
                        interfaces.append(iface)
                        # Find sibling implementations
                        for inc in graph.get_incoming_edges(iface.id, edge_kind):
                            sibling = graph.get_node(inc.source)
                            if sibling and sibling.id != sym.id:
                                siblings.append(sibling)

        # Test file
        test_file = find_test_file(file_path, graph)

        # DI registrations: incoming CALLS/USES_TYPE from container-like files
        di_keywords = {"container", "factory", "di", "module", "injector", "provider"}
        di_registrations: list[Node] = []
        for sym in symbols:
            if sym.kind != NodeKind.CLASS:
                continue
            for edge_kind in (EdgeKind.CALLS, EdgeKind.USES_TYPE):
                for edge in graph.get_incoming_edges(sym.id, edge_kind):
                    source = graph.get_node(edge.source)
                    if source and source.path:
                        path_lower = source.path.lower()
                        if any(kw in path_lower for kw in di_keywords):
                            di_registrations.append(source)

        # Import context
        imports_out: list[Node] = []
        for edge in graph.get_outgoing_edges(file_node.id, EdgeKind.IMPORTS):
            target = graph.get_node(edge.target)
            if target:
                imports_out.append(target)

        imports_in: list[Node] = []
        for edge in graph.get_incoming_edges(file_node.id, EdgeKind.IMPORTS):
            source = graph.get_node(edge.source)
            if source:
                imports_in.append(source)

        return WriteContextBundle(
            file_node=file_node,
            symbols=symbols,
            interfaces=interfaces,
            sibling_implementations=siblings,
            test_file=test_file,
            di_registrations=di_registrations,
            imports_outgoing=imports_out,
            imports_incoming=imports_in,
        )

    @staticmethod
    def _find_file_node(file_path: str, graph: CodeGraph) -> Optional[Node]:
        """Find a file node by exact or suffix match."""
        for node in graph.all_nodes():
            if node.kind != NodeKind.FILE:
                continue
            if node.path == file_path:
                return node
            if node.path and node.path.endswith(file_path):
                return node
            if file_path and node.path and os.path.basename(node.path) == os.path.basename(file_path):
                # Last resort: basename match (ambiguous but useful for short paths)
                pass
        # Try basename match as fallback
        basename = os.path.basename(file_path) if file_path else ""
        if basename:
            for node in graph.all_nodes():
                if node.kind == NodeKind.FILE and node.path and node.path.endswith(basename):
                    return node
        return None
