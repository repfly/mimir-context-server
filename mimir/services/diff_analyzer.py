"""Maps git diffs to code graph ChangeSets.

Parses unified diff format, resolves changed lines to existing graph nodes,
and extracts new symbols from added files using tree-sitter.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from mimir.domain.graph import CodeGraph
from mimir.domain.guardrails import ChangeSet
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.ports.parser import Parser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------

@dataclass
class _FileDiff:
    """Parsed representation of changes to a single file."""

    old_path: str
    new_path: str
    status: str  # "added", "modified", "deleted", "renamed"
    hunks: list[tuple[int, int]] = field(default_factory=list)  # (start, end) in new file
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)


# Regex patterns for unified diff parsing
_DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NEW_FILE = re.compile(r"^new file mode")
_DELETED_FILE = re.compile(r"^deleted file mode")
_RENAME_FROM = re.compile(r"^rename from (.*)")
_BINARY_PATCH = re.compile(r"^Binary files")


class DiffAnalyzer:
    """Maps a git diff to a ChangeSet against the code graph."""

    def __init__(self, parser: Parser) -> None:
        self._parser = parser

    async def analyze(self, graph: CodeGraph, diff_text: str) -> ChangeSet:
        """Parse unified diff, map to graph nodes, return ChangeSet.

        Fail-open: parse errors produce an empty ChangeSet with a logged warning.
        """
        if not diff_text or not diff_text.strip():
            return ChangeSet()

        try:
            file_diffs = self._parse_unified_diff(diff_text)
        except Exception:
            logger.warning("Failed to parse diff text", exc_info=True)
            return ChangeSet()

        modified_nodes: list[str] = []
        new_nodes: list[Node] = []
        new_edges: list[Edge] = []
        removed_edges: list[Edge] = []
        affected_files: list[str] = []

        for fd in file_diffs:
            path = fd.new_path if fd.status != "deleted" else fd.old_path
            affected_files.append(path)

            if fd.status == "deleted":
                # Find nodes in the deleted file and mark as modified
                file_nodes = self._find_nodes_in_file(graph, fd.old_path)
                modified_nodes.extend(n.id for n in file_nodes)
                continue

            if fd.status == "added":
                # Extract new symbols from added files
                extracted = await self._extract_new_symbols(
                    fd.new_path, fd.added_lines, graph,
                )
                new_nodes.extend(extracted[0])
                new_edges.extend(extracted[1])
                continue

            # Modified file: map changed lines to existing nodes
            node_ids = self._map_lines_to_nodes(graph, fd.new_path, fd.hunks)
            modified_nodes.extend(node_ids)

            # Detect new edges from added import/call lines
            detected = self._detect_edge_changes(
                graph, fd.new_path, fd.added_lines, fd.removed_lines,
            )
            new_edges.extend(detected[0])
            removed_edges.extend(detected[1])

        return ChangeSet(
            modified_nodes=tuple(dict.fromkeys(modified_nodes)),  # dedupe preserving order
            new_nodes=tuple(new_nodes),
            new_edges=tuple(new_edges),
            removed_edges=tuple(removed_edges),
            affected_files=tuple(dict.fromkeys(affected_files)),
        )

    # ------------------------------------------------------------------
    # Diff parsing
    # ------------------------------------------------------------------

    def _parse_unified_diff(self, diff_text: str) -> list[_FileDiff]:
        """Extract per-file changes from unified diff format."""
        file_diffs: list[_FileDiff] = []
        current: Optional[_FileDiff] = None
        current_line = 0  # tracks line number in new file within current hunk
        hunk_start = 0

        for line in diff_text.splitlines():
            # New file header
            m = _DIFF_HEADER.match(line)
            if m:
                if current is not None:
                    self._finalize_hunk(current, hunk_start, current_line)
                    file_diffs.append(current)
                current = _FileDiff(
                    old_path=m.group(1),
                    new_path=m.group(2),
                    status="modified",
                )
                current_line = 0
                hunk_start = 0
                continue

            if current is None:
                continue

            # Binary file — skip
            if _BINARY_PATCH.match(line):
                current.status = "modified"
                continue

            # New/deleted file markers
            if _NEW_FILE.match(line):
                current.status = "added"
                continue
            if _DELETED_FILE.match(line):
                current.status = "deleted"
                continue
            if _RENAME_FROM.match(line):
                current.status = "renamed"
                continue

            # Hunk header
            m = _HUNK_HEADER.match(line)
            if m:
                self._finalize_hunk(current, hunk_start, current_line)
                hunk_start = int(m.group(1))
                current_line = hunk_start
                continue

            # Diff content lines
            if line.startswith("+") and not line.startswith("+++"):
                current.added_lines.append(line[1:])
                current_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                current.removed_lines.append(line[1:])
                # Removed lines don't advance current_line in new file
            else:
                # Context line
                current_line += 1

        # Finalize last file
        if current is not None:
            self._finalize_hunk(current, hunk_start, current_line)
            file_diffs.append(current)

        return file_diffs

    @staticmethod
    def _finalize_hunk(fd: _FileDiff, start: int, end: int) -> None:
        """Record a hunk's line range if non-empty."""
        if start > 0 and end > start:
            fd.hunks.append((start, end))

    # ------------------------------------------------------------------
    # Node mapping
    # ------------------------------------------------------------------

    def _map_lines_to_nodes(
        self,
        graph: CodeGraph,
        file_path: str,
        line_ranges: list[tuple[int, int]],
    ) -> list[str]:
        """Find graph node IDs that overlap with changed line ranges."""
        if not line_ranges:
            return []

        # Find all symbol nodes in this file
        file_nodes = self._find_nodes_in_file(graph, file_path)
        matched: list[str] = []

        for node in file_nodes:
            if node.start_line is None or node.end_line is None:
                continue
            for hunk_start, hunk_end in line_ranges:
                # Check overlap: node range intersects hunk range
                if node.start_line <= hunk_end and node.end_line >= hunk_start:
                    matched.append(node.id)
                    break

        return matched

    @staticmethod
    def _find_nodes_in_file(graph: CodeGraph, file_path: str) -> list[Node]:
        """Find all nodes whose path matches (suffix match)."""
        result: list[Node] = []
        for node in graph.all_nodes():
            if node.path and (
                node.path == file_path
                or node.path.endswith("/" + file_path)
                or file_path.endswith("/" + node.path)
            ):
                result.append(node)
        return result

    # ------------------------------------------------------------------
    # New symbol extraction
    # ------------------------------------------------------------------

    async def _extract_new_symbols(
        self,
        file_path: str,
        added_lines: list[str],
        graph: CodeGraph,
    ) -> tuple[list[Node], list[Edge]]:
        """Run tree-sitter on new file content to find new symbols."""
        if not added_lines:
            return [], []

        content = "\n".join(added_lines)
        nodes: list[Node] = []
        edges: list[Edge] = []

        # Write to temp file for parser
        suffix = os.path.splitext(file_path)[1]
        if not suffix:
            return [], []

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, delete=False,
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            try:
                symbols = await self._parser.parse_file(tmp_path)
            finally:
                os.unlink(tmp_path)
        except Exception:
            logger.warning("Failed to parse new file %s", file_path, exc_info=True)
            return [], []

        # Infer repo from existing graph nodes or use file path
        repo = self._infer_repo(graph, file_path)

        for sym in symbols:
            node_id = f"{repo}:{file_path}::{sym.name}"
            kind = self._map_symbol_kind(sym.kind)
            node = Node(
                id=node_id,
                repo=repo,
                kind=kind,
                name=sym.name,
                path=file_path,
                start_line=sym.start_line,
                end_line=sym.end_line,
                raw_code=sym.code,
                signature=sym.signature,
                docstring=sym.docstring,
            )
            nodes.append(node)

            # Create edges for imports/calls detected by parser
            for imp in sym.imports:
                edges.append(Edge(
                    source=node_id, target=imp, kind=EdgeKind.IMPORTS,
                ))
            for call in sym.calls:
                edges.append(Edge(
                    source=node_id, target=call, kind=EdgeKind.CALLS,
                ))

        return nodes, edges

    # ------------------------------------------------------------------
    # Edge change detection
    # ------------------------------------------------------------------

    def _detect_edge_changes(
        self,
        graph: CodeGraph,
        file_path: str,
        added_lines: list[str],
        removed_lines: list[str],
    ) -> tuple[list[Edge], list[Edge]]:
        """Detect new/removed edges from added/removed import lines.

        Uses simple pattern matching for common import statements.
        """
        new_edges: list[Edge] = []
        removed_edges: list[Edge] = []

        file_nodes = self._find_nodes_in_file(graph, file_path)
        if not file_nodes:
            return new_edges, removed_edges

        # Use the file node as source for import edges
        file_node = None
        for n in file_nodes:
            if n.kind == NodeKind.FILE:
                file_node = n
                break
        if file_node is None and file_nodes:
            file_node = file_nodes[0]
        if file_node is None:
            return new_edges, removed_edges

        added_imports = self._extract_import_targets(added_lines)
        removed_imports = self._extract_import_targets(removed_lines)

        for target in added_imports:
            new_edges.append(Edge(
                source=file_node.id, target=target, kind=EdgeKind.IMPORTS,
            ))
        for target in removed_imports:
            removed_edges.append(Edge(
                source=file_node.id, target=target, kind=EdgeKind.IMPORTS,
            ))

        return new_edges, removed_edges

    @staticmethod
    def _extract_import_targets(lines: list[str]) -> list[str]:
        """Extract import module names from source lines."""
        targets: list[str] = []
        for line in lines:
            stripped = line.strip()
            # JS/TS: import ... from "X"  (check before Python pattern)
            m = re.match(r'^import\s+.*\s+from\s+["\']([^"\']+)["\']', stripped)
            if m:
                targets.append(m.group(1))
                continue
            # Go: import "X"
            m = re.match(r'^import\s+["\']([^"\']+)["\']', stripped)
            if m:
                targets.append(m.group(1))
                continue
            # Python: from X import Y / import X
            m = re.match(r"^from\s+(\S+)\s+import\s+", stripped)
            if m:
                targets.append(m.group(1))
                continue
            m = re.match(r"^import\s+(\S+)", stripped)
            if m:
                targets.append(m.group(1))
                continue
        return targets

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_repo(graph: CodeGraph, file_path: str) -> str:
        """Infer repository name from graph context or file path."""
        repos = graph.repos
        if len(repos) == 1:
            return repos[0]
        # Try to match file path to a repo
        for node in graph.all_nodes():
            if node.path and file_path.startswith(node.path.split("/")[0]):
                return node.repo
        return repos[0] if repos else "unknown"

    @staticmethod
    def _map_symbol_kind(kind_str: str) -> NodeKind:
        """Map parser symbol kind string to NodeKind enum."""
        mapping = {
            "function": NodeKind.FUNCTION,
            "method": NodeKind.METHOD,
            "class": NodeKind.CLASS,
            "type": NodeKind.TYPE,
            "constant": NodeKind.CONSTANT,
            "module": NodeKind.MODULE,
            "file": NodeKind.FILE,
        }
        return mapping.get(kind_str.lower(), NodeKind.FUNCTION)
