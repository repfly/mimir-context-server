"""Evaluates changes against architectural rules using the code graph.

Fail-open on rule evaluation errors (log and continue).
Fail-closed on rule loading errors (handled by guardrails_config).
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
from typing import Optional

import networkx as nx

from mimir.domain.graph import CodeGraph
from mimir.domain.guardrails import (
    ApprovalStatus,
    ChangeSet,
    CouplingMetric,
    CycleScope,
    GuardrailResult,
    Rule,
    RuleType,
    Severity,
    Violation,
)
from mimir.domain.models import EdgeKind, NodeKind
from mimir.services.guardrail.diff_analyzer import DiffAnalyzer
from mimir.services.impact import ImpactService
from mimir.services.quality import QualityService

logger = logging.getLogger(__name__)

#: Edge kinds considered as dependencies (excludes CONTAINS).
_DEPENDENCY_EDGE_KINDS = frozenset({
    EdgeKind.CALLS,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
    EdgeKind.IMPLEMENTS,
    EdgeKind.USES_TYPE,
    EdgeKind.READS_CONFIG,
    EdgeKind.EXPOSES_API,
    EdgeKind.API_CALLS,
    EdgeKind.SHARED_LIB,
    EdgeKind.PROTO_DEFINES,
})

#: Maximum cycles to report before bailing.
_MAX_CYCLES = 100


class GuardrailService:
    """Evaluates changes against architectural rules using the code graph."""

    def __init__(
        self,
        *,
        impact_service: ImpactService,
        quality_service: QualityService,
        diff_analyzer: DiffAnalyzer,
    ) -> None:
        self._impact = impact_service
        self._quality = quality_service
        self._diff = diff_analyzer

        self._handlers = {
            RuleType.DEPENDENCY_BAN: self._check_dependency_ban,
            RuleType.CYCLE_DETECTION: self._check_cycle_detection,
            RuleType.METRIC_THRESHOLD: self._check_metric_threshold,
            RuleType.IMPACT_THRESHOLD: self._check_impact_threshold,
            RuleType.FILE_SCOPE_BAN: self._check_file_scope_ban,
        }

    async def evaluate(
        self,
        graph: CodeGraph,
        diff_text: str,
        rules: list[Rule],
        agent_policy: object | None = None,
    ) -> GuardrailResult:
        """Full evaluation pipeline: parse diff -> build ChangeSet -> check rules."""
        change = await self._diff.analyze(graph, diff_text)

        all_violations: list[Violation] = []

        for rule in rules:
            handler = self._handlers.get(rule.type)
            if handler is None:
                logger.warning("No handler for rule type %s", rule.type)
                continue
            try:
                violations = handler(graph, change, rule)
                all_violations.extend(violations)
            except Exception:
                logger.warning(
                    "Rule %s evaluation failed (fail-open)", rule.id, exc_info=True,
                )

        passed = not any(
            v.severity in (Severity.ERROR, Severity.BLOCK) for v in all_violations
        )

        warning_count = sum(1 for v in all_violations if v.severity == Severity.WARNING)
        error_count = sum(1 for v in all_violations if v.severity == Severity.ERROR)
        block_count = sum(1 for v in all_violations if v.severity == Severity.BLOCK)

        parts: list[str] = []
        if passed:
            parts.append("All checks passed")
        else:
            parts.append("Violations found")
        if error_count:
            parts.append(f"{error_count} error(s)")
        if block_count:
            parts.append(f"{block_count} block(s)")
        if warning_count:
            parts.append(f"{warning_count} warning(s)")

        return GuardrailResult(
            violations=tuple(all_violations),
            passed=passed,
            summary=". ".join(parts),
            change_set=change,
            rules_evaluated=len(rules),
        )

    # ------------------------------------------------------------------
    # Rule handlers
    # ------------------------------------------------------------------

    def _check_dependency_ban(
        self, graph: CodeGraph, change: ChangeSet, rule: Rule,
    ) -> list[Violation]:
        """Check if new edges match a banned source->target pattern."""
        source_pattern = rule.config["source_pattern"]
        target_pattern = rule.config["target_pattern"]
        edge_kinds_filter = rule.config.get("edge_kind")
        cross_repo_only = rule.config.get("cross_repo_only", False)

        # Normalize edge_kind filter
        allowed_kinds: Optional[set[EdgeKind]] = None
        if edge_kinds_filter:
            if isinstance(edge_kinds_filter, list):
                allowed_kinds = set()
                for k in edge_kinds_filter:
                    try:
                        allowed_kinds.add(EdgeKind(k))
                    except ValueError:
                        pass
            elif isinstance(edge_kinds_filter, str):
                try:
                    allowed_kinds = {EdgeKind(edge_kinds_filter)}
                except ValueError:
                    pass

        violations: list[Violation] = []

        for edge in change.new_edges:
            # Filter by edge kind
            if allowed_kinds and edge.kind not in allowed_kinds:
                continue

            # Filter cross-repo only
            if cross_repo_only and not edge.is_cross_repo:
                continue

            # Resolve source and target nodes
            source_node = graph.get_node(edge.source)
            target_node = graph.get_node(edge.target)

            source_path = source_node.path if source_node else edge.source
            target_path = target_node.path if target_node else edge.target

            if source_path and target_path:
                if fnmatch.fnmatch(source_path, source_pattern) and \
                   fnmatch.fnmatch(target_path, target_pattern):
                    violations.append(Violation(
                        rule_id=rule.id,
                        rule_description=rule.description,
                        severity=rule.severity,
                        message=(
                            f"Banned dependency: {source_path} -> {target_path} "
                            f"({edge.kind.value})"
                        ),
                        evidence=(
                            f"source: {edge.source}",
                            f"target: {edge.target}",
                            f"edge_kind: {edge.kind.value}",
                        ),
                        file_path=source_path,
                        suggested_fix=(
                            f"Remove the dependency from {source_path} to "
                            f"{target_path}. Consider using a port/interface instead."
                        ),
                    ))

        return violations

    def _check_cycle_detection(
        self, graph: CodeGraph, change: ChangeSet, rule: Rule,
    ) -> list[Violation]:
        """Detect cycles introduced by the change."""
        try:
            scope = CycleScope(rule.config["scope"])
        except (KeyError, ValueError):
            logger.warning("Rule %s has invalid cycle scope %r", rule.id, rule.config.get("scope"))
            return []
        edge_kinds_config = rule.config.get("edge_kinds", [])

        # Determine which edge kinds to include
        if edge_kinds_config:
            filter_kinds = set()
            for k in edge_kinds_config:
                try:
                    filter_kinds.add(EdgeKind(k))
                except ValueError:
                    pass
        else:
            if scope is CycleScope.CROSS_REPO:
                filter_kinds = {
                    EdgeKind.API_CALLS, EdgeKind.SHARED_LIB, EdgeKind.PROTO_DEFINES,
                }
            else:
                filter_kinds = {EdgeKind.IMPORTS}

        # Build scoped subgraph
        sub = nx.DiGraph()

        for edge in graph.all_edges():
            if edge.kind in filter_kinds:
                sub.add_edge(edge.source, edge.target, edge_obj=edge)

        # Track new edges for filtering
        new_edge_set: set[tuple[str, str]] = set()
        for edge in change.new_edges:
            if edge.kind in filter_kinds:
                sub.add_edge(edge.source, edge.target, edge_obj=edge)
                new_edge_set.add((edge.source, edge.target))

        if not new_edge_set:
            return []

        # Find cycles containing at least one new edge
        violations: list[Violation] = []
        cycle_count = 0

        try:
            for cycle in nx.simple_cycles(sub):
                if cycle_count >= _MAX_CYCLES:
                    break

                # Check if cycle contains a new edge
                cycle_edges = set()
                for i in range(len(cycle)):
                    src = cycle[i]
                    tgt = cycle[(i + 1) % len(cycle)]
                    cycle_edges.add((src, tgt))

                if cycle_edges & new_edge_set:
                    cycle_count += 1
                    cycle_str = " -> ".join(cycle) + " -> " + cycle[0]
                    violations.append(Violation(
                        rule_id=rule.id,
                        rule_description=rule.description,
                        severity=rule.severity,
                        message=f"Circular dependency detected: {cycle_str}",
                        evidence=tuple(
                            f"{s} -> {t}" for s, t in cycle_edges
                            if (s, t) in new_edge_set
                        ),
                        suggested_fix="Break the cycle by removing or inverting one of the dependencies.",
                    ))
        except Exception:
            logger.warning("Cycle detection aborted", exc_info=True)

        return violations

    def _check_metric_threshold(
        self, graph: CodeGraph, change: ChangeSet, rule: Rule,
    ) -> list[Violation]:
        """Check coupling metrics against thresholds."""
        try:
            metric = CouplingMetric(rule.config["metric"])
        except (KeyError, ValueError):
            logger.warning("Rule %s has invalid coupling metric %r", rule.id, rule.config.get("metric"))
            return []
        threshold = rule.config["threshold"]
        target_pattern = rule.config.get("target_pattern")

        violations: list[Violation] = []

        for node_id in change.modified_nodes:
            node = graph.get_node(node_id)
            if node is None:
                continue

            # Filter by target_pattern if specified
            if target_pattern and node.path:
                if not fnmatch.fnmatch(node.path, target_pattern):
                    continue

            # Get all dependency edges (exclude CONTAINS)
            incoming = [
                e for e in graph.get_incoming_edges(node_id)
                if e.kind in _DEPENDENCY_EDGE_KINDS
            ]
            outgoing = [
                e for e in graph.get_outgoing_edges(node_id)
                if e.kind in _DEPENDENCY_EDGE_KINDS
            ]

            ca = len(incoming)  # afferent coupling
            ce = len(outgoing)  # efferent coupling

            if metric is CouplingMetric.AFFERENT_COUPLING:
                value = ca
            elif metric is CouplingMetric.EFFERENT_COUPLING:
                value = ce
            elif metric is CouplingMetric.INSTABILITY:
                value = ce / (ca + ce) if (ca + ce) > 0 else 0.0
            else:
                continue

            if value > threshold:
                violations.append(Violation(
                    rule_id=rule.id,
                    rule_description=rule.description,
                    severity=rule.severity,
                    message=(
                        f"{metric.value} for {node.name} is {value} "
                        f"(threshold: {threshold})"
                    ),
                    evidence=(
                        f"node: {node_id}",
                        f"afferent_coupling: {ca}",
                        f"efferent_coupling: {ce}",
                    ),
                    file_path=node.path,
                    suggested_fix=f"Reduce {metric.value} below {threshold} by refactoring dependencies.",
                ))

        return violations

    def _check_impact_threshold(
        self, graph: CodeGraph, change: ChangeSet, rule: Rule,
    ) -> list[Violation]:
        """Check blast radius against maximum allowed impact."""
        max_impact = rule.config["max_impact"]
        max_hops = rule.config.get("max_hops", 3)
        target_kinds_config = rule.config.get("target_kind", [])
        target_pattern = rule.config.get("target_pattern")

        # Parse target_kind filter
        target_kinds: Optional[set[NodeKind]] = None
        if target_kinds_config:
            target_kinds = set()
            for k in target_kinds_config:
                try:
                    target_kinds.add(NodeKind(k))
                except ValueError:
                    pass

        violations: list[Violation] = []

        for node_id in change.modified_nodes:
            node = graph.get_node(node_id)
            if node is None:
                continue

            # Filter by target kind
            if target_kinds and node.kind not in target_kinds:
                continue

            # Filter by target_pattern
            if target_pattern and node.path:
                if not fnmatch.fnmatch(node.path, target_pattern):
                    continue

            # Run impact analysis
            result = self._impact.analyze(graph, node_id=node_id, max_hops=max_hops)
            if result is None:
                continue

            if result.total_impact_count > max_impact:
                callers_str = ", ".join(
                    n.name for n in result.direct_callers[:5]
                )
                violations.append(Violation(
                    rule_id=rule.id,
                    rule_description=rule.description,
                    severity=rule.severity,
                    message=(
                        f"Blast radius for {node.name} is {result.total_impact_count} "
                        f"(max allowed: {max_impact})"
                    ),
                    evidence=(
                        f"node: {node_id}",
                        f"total_impact: {result.total_impact_count}",
                        f"direct_callers: {callers_str}" if callers_str else "no direct callers",
                    ),
                    file_path=node.path,
                    suggested_fix=(
                        f"This change affects {result.total_impact_count} downstream nodes. "
                        f"Consider splitting the change or adding an abstraction layer."
                    ),
                ))

        return violations

    def _check_file_scope_ban(
        self, graph: CodeGraph, change: ChangeSet, rule: Rule,
    ) -> list[Violation]:
        """Enforce file scope restrictions (bounded autonomy)."""
        path_pattern = rule.config["path_pattern"]

        violations: list[Violation] = []

        for file_path in change.affected_files:
            if fnmatch.fnmatch(file_path, path_pattern):
                msg = f"File {file_path} matches protected pattern '{path_pattern}'"

                violations.append(Violation(
                    rule_id=rule.id,
                    rule_description=rule.description,
                    severity=rule.severity,
                    message=msg,
                    file_path=file_path,
                    suggested_fix="Request human review before modifying this file.",
                ))

        return violations


# ------------------------------------------------------------------
# Approval matching (pure function)
# ------------------------------------------------------------------

def apply_approvals(
    result: GuardrailResult,
    *,
    approved_rule_ids: frozenset[str],
    reason: str,
) -> GuardrailResult:
    """Annotate BLOCK violations with approval status from the HEAD trailer.

    The approval model is intentionally stateless: whatever the HEAD commit
    of the branch declares via its ``Mimir-Approved:`` trailer is the
    source of truth. Pushing a new commit that lacks the trailer
    auto-invalidates the approval.

    A BLOCK violation is ``approved`` iff its rule id is in
    ``approved_rule_ids`` **and** ``reason`` is non-empty. There is no
    self-approval guard — whoever commits the trailer is trusted.
    """
    effective_approved = approved_rule_ids if reason else frozenset()

    new_violations: list[Violation] = []
    pending_count = 0

    for v in result.violations:
        if v.severity != Severity.BLOCK:
            new_violations.append(v)
            continue

        if v.rule_id in effective_approved:
            new_violations.append(
                dataclasses.replace(v, approval_status=ApprovalStatus.APPROVED)
            )
        else:
            new_violations.append(
                dataclasses.replace(v, approval_status=ApprovalStatus.PENDING)
            )
            pending_count += 1

    # Recompute passed: errors still fail; only unapproved blocks fail
    has_errors = any(v.severity == Severity.ERROR for v in new_violations)
    passed = not has_errors and pending_count == 0

    # Build summary
    error_count = sum(1 for v in new_violations if v.severity == Severity.ERROR)
    warning_count = sum(1 for v in new_violations if v.severity == Severity.WARNING)
    approved_count = sum(
        1 for v in new_violations
        if v.severity == Severity.BLOCK and v.approval_status is ApprovalStatus.APPROVED
    )

    parts: list[str] = []
    if passed:
        parts.append("All checks passed")
    else:
        parts.append("Violations found")
    if error_count:
        parts.append(f"{error_count} error(s)")
    if pending_count:
        parts.append(f"{pending_count} block(s) pending")
    if approved_count:
        parts.append(f"{approved_count} block(s) approved")
    if warning_count:
        parts.append(f"{warning_count} warning(s)")

    return GuardrailResult(
        violations=tuple(new_violations),
        passed=passed,
        summary=". ".join(parts),
        change_set=result.change_set,
        rules_evaluated=result.rules_evaluated,
    )
