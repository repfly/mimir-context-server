"""Quality service — node connectivity scoring and gap detection.

Computes a connectivity quality score for each node based on how
well-connected and well-resolved its edges are in the graph.  Also
detects "gaps" — nodes with missing expected connections that may
indicate under-indexed or poorly-resolved areas of the codebase.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from mimir.domain.graph import CodeGraph
from mimir.domain.models import (
    CONTAINER_KINDS,
    SYMBOL_KINDS,
    EdgeKind,
    Node,
    NodeKind,
)

logger = logging.getLogger(__name__)


# -- Expected edge patterns per node kind ------------------------------------
# Maps NodeKind to the set of outgoing EdgeKinds we'd *expect* a well-connected
# node of that kind to have.  Missing expected edges raise the gap score.
# Note: CLASS is intentionally absent — not all classes have methods (enums,
# protocols, dataclasses, exception classes).  Class expectations are handled
# dynamically in _get_expected_edges().

_EXPECTED_EDGES: dict[NodeKind, set[EdgeKind]] = {
    NodeKind.FILE: {EdgeKind.CONTAINS, EdgeKind.IMPORTS},
    NodeKind.FUNCTION: set(),  # leaf; no strict expectations
    NodeKind.METHOD: set(),
    NodeKind.CLASS: set(),  # evaluated dynamically
    NodeKind.TYPE: set(),
    NodeKind.MODULE: {EdgeKind.CONTAINS},
}

# Patterns that identify classes which naturally lack CONTAINS edges.
# These are not gaps — they're valid structural patterns.
_THIN_CLASS_PATTERNS = frozenset({
    # Common base/interface/protocol suffixes
    "error", "exception", "protocol", "interface", "base", "abstract",
    "mixin", "enum", "kind", "type", "result", "config", "options",
    "params", "args", "kwargs", "meta", "info", "status", "state",
    "event", "signal", "spec", "schema", "dto", "vo",
})


@dataclass
class GapReport:
    """A single node flagged as having missing connections."""

    node_id: str
    node_name: str
    node_kind: str
    repo: str
    path: Optional[str]
    quality_score: float
    missing_edges: list[str]
    reason: str

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "node_kind": self.node_kind,
            "repo": self.repo,
            "path": self.path,
            "quality_score": round(self.quality_score, 4),
            "missing_edges": self.missing_edges,
            "reason": self.reason,
        }


@dataclass
class QualityOverview:
    """Summary of graph quality across all nodes."""

    total_nodes: int = 0
    scored_nodes: int = 0
    avg_quality: float = 0.0
    gap_count: int = 0
    gaps: list[GapReport] = field(default_factory=list)
    quality_distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_nodes": self.total_nodes,
            "scored_nodes": self.scored_nodes,
            "avg_quality": round(self.avg_quality, 4),
            "gap_count": self.gap_count,
            "gaps": [g.to_dict() for g in self.gaps],
            "quality_distribution": self.quality_distribution,
        }

    def format_for_llm(self) -> str:
        parts = [
            f"**Graph Quality Overview**",
            f"- Scored nodes: {self.scored_nodes} / {self.total_nodes}",
            f"- Average quality: {self.avg_quality:.2f}",
            f"- Gaps detected: {self.gap_count}",
        ]
        if self.quality_distribution:
            parts.append(f"- Distribution: {self.quality_distribution}")
        if self.gaps:
            parts.append("")
            parts.append("**Gaps (nodes with missing connections):**")
            for gap in self.gaps[:20]:
                parts.append(
                    f"- `{gap.node_id}` (quality={gap.quality_score:.2f}) — {gap.reason}"
                )
            if len(self.gaps) > 20:
                parts.append(f"  … and {len(self.gaps) - 20} more")
        return "\n".join(parts)


class QualityService:
    """Computes node connectivity quality scores and detects graph gaps."""

    def __init__(self, gap_threshold: float = 0.3) -> None:
        self._gap_threshold = gap_threshold

    def compute_quality_score(self, node: Node, graph: CodeGraph) -> float:
        """Compute a connectivity quality score in [0, 1] for a single node.

        The score combines:
        - **Edge density**: how many meaningful (non-CONTAINS) edges the node has
        - **Embedding presence**: whether the node has an embedding vector
        - **Content completeness**: whether the node has code/summary content
        - **Expected edge coverage**: fraction of expected edge kinds present

        Higher score = better connected and more complete node.
        """
        scores: list[float] = []

        # 1. Edge density — how many dependency edges does this node have?
        outgoing = graph.get_outgoing_edges(node.id)
        incoming = graph.get_incoming_edges(node.id)

        # Count non-structural edges (exclude CONTAINS for density)
        dep_out = [e for e in outgoing if e.kind != EdgeKind.CONTAINS]
        dep_in = [e for e in incoming if e.kind != EdgeKind.CONTAINS]
        # For classes, also count CONTAINS as meaningful (methods are children)
        contains_out = [e for e in outgoing if e.kind == EdgeKind.CONTAINS]
        total_meaningful = len(dep_out) + len(dep_in)

        if node.kind == NodeKind.CLASS:
            # Classes: connectivity = dependency edges + having children
            # A class with methods (CONTAINS) and incoming USES_TYPE is well-connected.
            # A thin class (enum, protocol, exception) with just code is also fine.
            thin = self._is_thin_class(node)
            if thin:
                # Thin classes: embedding + content is enough, edges are bonus
                edge_density = min(1.0, 0.6 + total_meaningful * 0.1)
            else:
                # Regular classes: children + deps matter
                child_count = len(contains_out)
                edge_density = 1.0 - 1.0 / (1.0 + (total_meaningful + child_count) / 4.0)
        elif node.is_symbol:
            # Sigmoid-like normalization: 5 deps → ~0.5, 15 → ~0.75
            edge_density = 1.0 - 1.0 / (1.0 + total_meaningful / 5.0)
        elif node.kind == NodeKind.FILE:
            # Files should have CONTAINS + IMPORTS
            edge_density = 1.0 - 1.0 / (1.0 + len(contains_out) / 3.0)
        else:
            edge_density = 1.0 - 1.0 / (1.0 + total_meaningful / 3.0)
        scores.append(edge_density)

        # 2. Embedding presence
        has_embedding = 1.0 if node.embedding else 0.0
        scores.append(has_embedding)

        # 3. Content completeness
        has_code = 1.0 if node.raw_code else 0.0
        has_summary = 1.0 if node.summary else 0.0
        has_docstring = 1.0 if node.docstring else 0.0
        content_score = max(has_code, has_summary)  # at least one is needed
        if node.is_symbol:
            # Symbols get a bonus for having docstrings
            content_score = 0.7 * content_score + 0.3 * has_docstring
        scores.append(content_score)

        # 4. Expected edge coverage
        expected = self._get_expected_edges(node, graph)
        if expected:
            present_kinds = {e.kind for e in outgoing}
            coverage = len(expected & present_kinds) / len(expected)
        else:
            coverage = 1.0  # no expectations → perfect coverage
        scores.append(coverage)

        # Weighted combination
        if node.kind == NodeKind.CLASS and self._is_thin_class(node):
            # Thin classes: content and embedding matter most, edges are bonus
            weights = [0.15, 0.35, 0.35, 0.15]
        elif node.is_symbol:
            # For symbols: edge density matters most
            weights = [0.40, 0.25, 0.20, 0.15]
        elif node.kind == NodeKind.FILE:
            # For files: content and structure matter most
            weights = [0.25, 0.20, 0.25, 0.30]
        else:
            weights = [0.25, 0.25, 0.25, 0.25]

        return sum(w * s for w, s in zip(weights, scores))

    @staticmethod
    def _is_thin_class(node: Node) -> bool:
        """Check if a class is a thin structural type that naturally lacks children.

        Enums, protocols, dataclasses, exception classes, and small type
        definitions are valid classes that shouldn't be penalized for missing
        CONTAINS edges.
        """
        name_lower = node.name.lower()

        # Check against known thin-class patterns
        for pattern in _THIN_CLASS_PATTERNS:
            if pattern in name_lower:
                return True

        # Small classes (< 15 lines) are often thin types
        if node.start_line and node.end_line:
            if (node.end_line - node.start_line) < 15:
                return True

        # Classes with very short code are likely thin
        if node.raw_code and len(node.raw_code) < 300:
            return True

        return False

    @staticmethod
    def _get_expected_edges(node: Node, graph: CodeGraph) -> set[EdgeKind]:
        """Get the expected outgoing edge kinds for a node, accounting for context."""
        base = _EXPECTED_EDGES.get(node.kind, set())

        # For files: don't require IMPORTS if the file has no symbol children
        # (e.g., __init__.py, config files)
        if node.kind == NodeKind.FILE and base:
            children = graph.get_children(node.id)
            if not children:
                # Empty file — only expect what's actually possible
                return set()
            # If file has children but no imports, that's okay for small files
            if len(children) <= 2:
                return {EdgeKind.CONTAINS}

        return base

    def compute_quality_scores(
        self, graph: CodeGraph, *, repos: Optional[list[str]] = None,
    ) -> dict[str, float]:
        """Compute quality scores for all nodes in the graph.

        Returns a dict of {node_id: quality_score}.
        """
        scores: dict[str, float] = {}
        for node in graph.all_nodes():
            if repos and node.repo not in repos:
                continue
            scores[node.id] = self.compute_quality_score(node, graph)
        return scores

    def detect_gaps(
        self,
        graph: CodeGraph,
        *,
        repos: Optional[list[str]] = None,
        threshold: Optional[float] = None,
        top_n: int = 50,
    ) -> QualityOverview:
        """Detect nodes with missing or weak connections.

        Returns a QualityOverview with gap reports for nodes below the
        quality threshold.
        """
        threshold = threshold if threshold is not None else self._gap_threshold

        overview = QualityOverview()
        all_scores: list[float] = []
        gaps: list[GapReport] = []

        for node in graph.all_nodes():
            if repos and node.repo not in repos:
                continue
            # Skip repository-level nodes (always structural)
            if node.kind == NodeKind.REPOSITORY:
                continue

            overview.total_nodes += 1
            score = self.compute_quality_score(node, graph)
            all_scores.append(score)

            # Classify into quality buckets
            if score >= 0.7:
                bucket = "good"
            elif score >= 0.4:
                bucket = "moderate"
            else:
                bucket = "poor"
            overview.quality_distribution[bucket] = (
                overview.quality_distribution.get(bucket, 0) + 1
            )

            # Check for gaps
            if score < threshold:
                missing, reason = self._diagnose_gap(node, graph)
                gaps.append(GapReport(
                    node_id=node.id,
                    node_name=node.name,
                    node_kind=node.kind.value,
                    repo=node.repo,
                    path=node.path,
                    quality_score=score,
                    missing_edges=missing,
                    reason=reason,
                ))

        overview.scored_nodes = len(all_scores)
        overview.avg_quality = sum(all_scores) / len(all_scores) if all_scores else 0.0

        # Sort gaps by quality score (worst first), cap at top_n
        gaps.sort(key=lambda g: g.quality_score)
        overview.gaps = gaps[:top_n]
        overview.gap_count = len(gaps)

        return overview

    def _diagnose_gap(self, node: Node, graph: CodeGraph) -> tuple[list[str], str]:
        """Diagnose why a node has a low quality score.

        Returns (missing_edge_kinds, human_readable_reason).
        """
        issues: list[str] = []
        missing: list[str] = []

        # Check expected edges (using dynamic expectations)
        expected = self._get_expected_edges(node, graph)
        if expected:
            present_kinds = {e.kind for e in graph.get_outgoing_edges(node.id)}
            for ek in expected:
                if ek not in present_kinds:
                    missing.append(ek.value)

        # Check embedding
        if not node.embedding:
            issues.append("no embedding")

        # Check content
        if not node.raw_code and not node.summary:
            issues.append("no code or summary")

        # Check isolation (symbol with zero dependency edges)
        # Skip thin classes — they're legitimately isolated
        if node.is_symbol:
            if node.kind == NodeKind.CLASS and self._is_thin_class(node):
                pass  # thin classes being isolated is expected
            else:
                outgoing = graph.get_outgoing_edges(node.id)
                incoming = graph.get_incoming_edges(node.id)
                dep_edges = [
                    e for e in outgoing + incoming if e.kind != EdgeKind.CONTAINS
                ]
                if not dep_edges:
                    issues.append("isolated (no dependency edges)")

        if missing:
            issues.append(f"missing expected edges: {', '.join(missing)}")

        reason = "; ".join(issues) if issues else "low overall connectivity"
        return missing, reason
