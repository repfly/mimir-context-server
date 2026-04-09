"""Tests for the GuardrailService rule evaluation engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mimir.domain.graph import CodeGraph
from mimir.domain.guardrails import ChangeSet, GuardrailResult, Rule, RuleType, Severity, Violation
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.services.guardrail import GuardrailService, apply_approvals
from mimir.services.impact import ImpactResult, ImpactService
from mimir.services.quality import QualityService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_graph() -> CodeGraph:
    """Build a synthetic graph for guardrail tests."""
    graph = CodeGraph()

    # Files
    graph.add_node(Node(
        id="repo:src/domain/model.py", repo="repo", kind=NodeKind.FILE,
        name="model.py", path="src/domain/model.py",
    ))
    graph.add_node(Node(
        id="repo:src/infra/db.py", repo="repo", kind=NodeKind.FILE,
        name="db.py", path="src/infra/db.py",
    ))
    graph.add_node(Node(
        id="repo:src/adapters/api.py", repo="repo", kind=NodeKind.FILE,
        name="api.py", path="src/adapters/api.py",
    ))

    # Functions
    graph.add_node(Node(
        id="repo:src/domain/model.py::User", repo="repo", kind=NodeKind.CLASS,
        name="User", path="src/domain/model.py", start_line=5, end_line=30,
    ))
    graph.add_node(Node(
        id="repo:src/infra/db.py::save_user", repo="repo", kind=NodeKind.FUNCTION,
        name="save_user", path="src/infra/db.py", start_line=10, end_line=25,
    ))
    graph.add_node(Node(
        id="repo:src/adapters/api.py::handle", repo="repo",
        kind=NodeKind.API_ENDPOINT, name="handle",
        path="src/adapters/api.py", start_line=5, end_line=15,
    ))

    # Existing edges
    graph.add_edge(Edge(
        source="repo:src/adapters/api.py::handle",
        target="repo:src/domain/model.py::User",
        kind=EdgeKind.USES_TYPE,
    ))
    graph.add_edge(Edge(
        source="repo:src/infra/db.py::save_user",
        target="repo:src/domain/model.py::User",
        kind=EdgeKind.USES_TYPE,
    ))

    return graph


def _make_diff_analyzer_mock(change: ChangeSet) -> AsyncMock:
    mock = AsyncMock()
    mock.analyze = AsyncMock(return_value=change)
    return mock


def _make_service(change: ChangeSet, impact_result=None) -> GuardrailService:
    diff_analyzer = _make_diff_analyzer_mock(change)
    impact = MagicMock(spec=ImpactService)
    impact.analyze.return_value = impact_result
    quality = MagicMock(spec=QualityService)

    return GuardrailService(
        impact_service=impact,
        quality_service=quality,
        diff_analyzer=diff_analyzer,
    )


# ---------------------------------------------------------------------------
# Dependency ban tests
# ---------------------------------------------------------------------------


class TestDependencyBan:
    async def test_detects_banned_import(self):
        graph = _build_graph()
        change = ChangeSet(
            new_edges=(
                Edge(
                    source="repo:src/domain/model.py::User",
                    target="repo:src/infra/db.py::save_user",
                    kind=EdgeKind.IMPORTS,
                ),
            ),
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="no-domain-to-infra",
            type=RuleType.DEPENDENCY_BAN,
            description="Domain must not import infra",
            severity=Severity.ERROR,
            config={
                "source_pattern": "*/domain/*",
                "target_pattern": "*/infra/*",
            },
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert not result.passed
        assert len(result.violations) == 1
        assert "Banned dependency" in result.violations[0].message

    async def test_allows_non_banned_import(self):
        graph = _build_graph()
        change = ChangeSet(
            new_edges=(
                Edge(
                    source="repo:src/adapters/api.py::handle",
                    target="repo:src/domain/model.py::User",
                    kind=EdgeKind.IMPORTS,
                ),
            ),
            affected_files=("src/adapters/api.py",),
        )
        rule = Rule(
            id="no-domain-to-infra",
            type=RuleType.DEPENDENCY_BAN,
            description="Domain must not import infra",
            severity=Severity.ERROR,
            config={
                "source_pattern": "*/domain/*",
                "target_pattern": "*/infra/*",
            },
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert result.passed
        assert len(result.violations) == 0

    async def test_edge_kind_filter(self):
        graph = _build_graph()
        change = ChangeSet(
            new_edges=(
                Edge(
                    source="repo:src/domain/model.py::User",
                    target="repo:src/infra/db.py::save_user",
                    kind=EdgeKind.CALLS,  # Not IMPORTS
                ),
            ),
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="no-domain-to-infra-imports",
            type=RuleType.DEPENDENCY_BAN,
            description="No domain->infra imports",
            severity=Severity.ERROR,
            config={
                "source_pattern": "*/domain/*",
                "target_pattern": "*/infra/*",
                "edge_kind": ["imports"],  # Only imports, not calls
            },
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert result.passed  # CALLS not in filter


# ---------------------------------------------------------------------------
# Cycle detection tests
# ---------------------------------------------------------------------------


class TestCycleDetection:
    async def test_detects_new_cycle(self):
        graph = _build_graph()
        # Add edge A->B
        graph.add_edge(Edge(
            source="repo:src/domain/model.py",
            target="repo:src/infra/db.py",
            kind=EdgeKind.IMPORTS,
        ))
        # ChangeSet introduces B->A, creating a cycle
        change = ChangeSet(
            new_edges=(
                Edge(
                    source="repo:src/infra/db.py",
                    target="repo:src/domain/model.py",
                    kind=EdgeKind.IMPORTS,
                ),
            ),
            affected_files=("src/infra/db.py",),
        )
        rule = Rule(
            id="no-circular-modules",
            type=RuleType.CYCLE_DETECTION,
            description="No circular imports",
            severity=Severity.WARNING,
            config={"scope": "intra_repo", "edge_kinds": ["imports"]},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert len(result.violations) > 0
        assert "Circular dependency" in result.violations[0].message

    async def test_no_cycle_when_no_new_edges(self):
        graph = _build_graph()
        change = ChangeSet(
            modified_nodes=("repo:src/domain/model.py::User",),
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="no-circular-modules",
            type=RuleType.CYCLE_DETECTION,
            description="No circular imports",
            severity=Severity.WARNING,
            config={"scope": "intra_repo", "edge_kinds": ["imports"]},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert len(result.violations) == 0


# ---------------------------------------------------------------------------
# Metric threshold tests
# ---------------------------------------------------------------------------


class TestMetricThreshold:
    async def test_afferent_coupling_violation(self):
        graph = _build_graph()
        # User has 2 incoming USES_TYPE edges from the graph
        change = ChangeSet(
            modified_nodes=("repo:src/domain/model.py::User",),
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="max-inbound",
            type=RuleType.METRIC_THRESHOLD,
            description="Max 1 inbound dep",
            severity=Severity.WARNING,
            config={"metric": "afferent_coupling", "threshold": 1},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert len(result.violations) == 1
        assert "afferent_coupling" in result.violations[0].message

    async def test_below_threshold_passes(self):
        graph = _build_graph()
        change = ChangeSet(
            modified_nodes=("repo:src/domain/model.py::User",),
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="max-inbound",
            type=RuleType.METRIC_THRESHOLD,
            description="Max 10 inbound deps",
            severity=Severity.WARNING,
            config={"metric": "afferent_coupling", "threshold": 10},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert len(result.violations) == 0

    async def test_instability_metric(self):
        graph = _build_graph()
        # save_user has 1 outgoing (USES_TYPE), 0 incoming dependency edges
        # instability = 1/(0+1) = 1.0
        change = ChangeSet(
            modified_nodes=("repo:src/infra/db.py::save_user",),
            affected_files=("src/infra/db.py",),
        )
        rule = Rule(
            id="stability",
            type=RuleType.METRIC_THRESHOLD,
            description="Max instability 0.5",
            severity=Severity.WARNING,
            config={"metric": "instability", "threshold": 0.5},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert len(result.violations) == 1


# ---------------------------------------------------------------------------
# Impact threshold tests
# ---------------------------------------------------------------------------


class TestImpactThreshold:
    async def test_high_impact_violation(self):
        graph = _build_graph()
        target_node = graph.get_node("repo:src/adapters/api.py::handle")
        impact_result = ImpactResult(
            target=target_node,
            direct_callers=[],
            type_users=[],
            implementors=[],
            test_files=[],
            transitive={},
            total_impact_count=20,
        )
        change = ChangeSet(
            modified_nodes=("repo:src/adapters/api.py::handle",),
            affected_files=("src/adapters/api.py",),
        )
        rule = Rule(
            id="blast-radius",
            type=RuleType.IMPACT_THRESHOLD,
            description="Max 10 impact",
            severity=Severity.ERROR,
            config={
                "max_impact": 10,
                "target_kind": ["api_endpoint"],
            },
        )
        svc = _make_service(change, impact_result=impact_result)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert not result.passed
        assert len(result.violations) == 1
        assert "20" in result.violations[0].message

    async def test_below_impact_passes(self):
        graph = _build_graph()
        target_node = graph.get_node("repo:src/adapters/api.py::handle")
        impact_result = ImpactResult(
            target=target_node,
            direct_callers=[],
            type_users=[],
            implementors=[],
            test_files=[],
            transitive={},
            total_impact_count=5,
        )
        change = ChangeSet(
            modified_nodes=("repo:src/adapters/api.py::handle",),
            affected_files=("src/adapters/api.py",),
        )
        rule = Rule(
            id="blast-radius",
            type=RuleType.IMPACT_THRESHOLD,
            description="Max 10 impact",
            severity=Severity.ERROR,
            config={"max_impact": 10, "target_kind": ["api_endpoint"]},
        )
        svc = _make_service(change, impact_result=impact_result)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert result.passed

    async def test_kind_filter_skips_non_matching(self):
        graph = _build_graph()
        change = ChangeSet(
            modified_nodes=("repo:src/domain/model.py::User",),  # CLASS, not API_ENDPOINT
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="blast-radius",
            type=RuleType.IMPACT_THRESHOLD,
            description="Max 10 impact",
            severity=Severity.ERROR,
            config={"max_impact": 10, "target_kind": ["api_endpoint"]},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert result.passed  # User is CLASS, not API_ENDPOINT


# ---------------------------------------------------------------------------
# File scope ban tests
# ---------------------------------------------------------------------------


class TestFileScopeBan:
    async def test_banned_file_detected(self):
        graph = _build_graph()
        change = ChangeSet(
            affected_files=("src/auth/login.py",),
        )
        rule = Rule(
            id="protect-auth",
            type=RuleType.FILE_SCOPE_BAN,
            description="Auth requires review",
            severity=Severity.BLOCK,
            config={"path_pattern": "*/auth/*"},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert not result.passed
        assert len(result.violations) == 1
        assert result.violations[0].severity == Severity.BLOCK
        assert "protected pattern" in result.violations[0].message

    async def test_non_banned_file_passes(self):
        graph = _build_graph()
        change = ChangeSet(
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="protect-auth",
            type=RuleType.FILE_SCOPE_BAN,
            description="Auth requires review",
            severity=Severity.BLOCK,
            config={"path_pattern": "*/auth/*"},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        assert result.passed


# ---------------------------------------------------------------------------
# Combined / integration tests
# ---------------------------------------------------------------------------


class TestCombinedEvaluation:
    async def test_multiple_rules(self):
        graph = _build_graph()
        change = ChangeSet(
            modified_nodes=("repo:src/domain/model.py::User",),
            new_edges=(
                Edge(
                    source="repo:src/domain/model.py::User",
                    target="repo:src/infra/db.py::save_user",
                    kind=EdgeKind.IMPORTS,
                ),
            ),
            affected_files=("src/domain/model.py",),
        )
        rules = [
            Rule(
                id="no-domain-to-infra",
                type=RuleType.DEPENDENCY_BAN,
                description="No domain->infra",
                severity=Severity.ERROR,
                config={"source_pattern": "*/domain/*", "target_pattern": "*/infra/*"},
            ),
            Rule(
                id="max-inbound",
                type=RuleType.METRIC_THRESHOLD,
                description="Max 10 inbound",
                severity=Severity.WARNING,
                config={"metric": "afferent_coupling", "threshold": 10},
            ),
        ]
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", rules)

        # 1 error (dependency_ban) + 0 warnings (under threshold)
        assert not result.passed
        assert result.rules_evaluated == 2
        assert len(result.violations) == 1

    async def test_fail_open_on_handler_error(self):
        """If a rule handler raises, the service should continue with other rules."""
        graph = _build_graph()
        change = ChangeSet(
            affected_files=("src/auth/login.py",),
        )
        rules = [
            Rule(
                id="bad-rule",
                type=RuleType.METRIC_THRESHOLD,
                description="Will fail",
                severity=Severity.ERROR,
                # Missing required config keys — will cause handler error
                config={"metric": "nonexistent", "threshold": 5},
            ),
            Rule(
                id="protect-auth",
                type=RuleType.FILE_SCOPE_BAN,
                description="Auth check",
                severity=Severity.BLOCK,
                config={"path_pattern": "*/auth/*"},
            ),
        ]
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", rules)

        # The bad rule should fail silently, auth rule should still fire
        assert len(result.violations) >= 1
        assert any(v.rule_id == "protect-auth" for v in result.violations)

    async def test_warnings_only_passes(self):
        graph = _build_graph()
        change = ChangeSet(
            modified_nodes=("repo:src/domain/model.py::User",),
            affected_files=("src/domain/model.py",),
        )
        rule = Rule(
            id="max-inbound",
            type=RuleType.METRIC_THRESHOLD,
            description="Max 1 inbound",
            severity=Severity.WARNING,
            config={"metric": "afferent_coupling", "threshold": 1},
        )
        svc = _make_service(change)
        result = await svc.evaluate(graph, "fake diff", [rule])

        # Warnings don't block
        assert result.passed
        assert len(result.violations) == 1
        assert result.violations[0].severity == Severity.WARNING


# ---------------------------------------------------------------------------
# apply_approvals tests (HEAD-trailer model)
# ---------------------------------------------------------------------------


def _make_result(violations: tuple[Violation, ...], passed: bool = False) -> GuardrailResult:
    return GuardrailResult(
        violations=violations,
        passed=passed,
        summary="test",
        change_set=ChangeSet(),
        rules_evaluated=1,
    )


class TestApplyApprovals:
    def test_matching_approval_downgrades_block(self):
        violations = (
            Violation(rule_id="r1", rule_description="d", severity=Severity.BLOCK, message="m"),
        )
        new_result = apply_approvals(
            _make_result(violations),
            approved_rule_ids=frozenset({"r1"}),
            reason="legal signoff",
        )
        assert new_result.passed is True
        assert new_result.violations[0].approval_status == "approved"

    def test_unmatched_rule_stays_pending(self):
        violations = (
            Violation(rule_id="r1", rule_description="d", severity=Severity.BLOCK, message="m"),
        )
        new_result = apply_approvals(
            _make_result(violations),
            approved_rule_ids=frozenset({"r2"}),  # wrong rule
            reason="ok",
        )
        assert new_result.passed is False
        assert new_result.violations[0].approval_status == "pending"

    def test_no_trailer_marks_pending(self):
        violations = (
            Violation(rule_id="r1", rule_description="d", severity=Severity.BLOCK, message="m"),
        )
        new_result = apply_approvals(
            _make_result(violations),
            approved_rule_ids=frozenset(),
            reason="",
        )
        assert new_result.passed is False
        assert new_result.violations[0].approval_status == "pending"

    def test_empty_reason_voids_approval(self):
        violations = (
            Violation(rule_id="r1", rule_description="d", severity=Severity.BLOCK, message="m"),
        )
        new_result = apply_approvals(
            _make_result(violations),
            approved_rule_ids=frozenset({"r1"}),
            reason="",
        )
        assert new_result.passed is False
        assert new_result.violations[0].approval_status == "pending"

    def test_errors_still_fail_even_with_approval(self):
        violations = (
            Violation(rule_id="r1", rule_description="d", severity=Severity.ERROR, message="m"),
            Violation(rule_id="r2", rule_description="d", severity=Severity.BLOCK, message="m"),
        )
        new_result = apply_approvals(
            _make_result(violations),
            approved_rule_ids=frozenset({"r2"}),
            reason="ok",
        )
        assert new_result.passed is False
        assert new_result.violations[1].approval_status == "approved"

    def test_warnings_untouched(self):
        violations = (
            Violation(rule_id="r1", rule_description="d", severity=Severity.WARNING, message="m"),
        )
        new_result = apply_approvals(
            _make_result(violations, passed=True),
            approved_rule_ids=frozenset(),
            reason="",
        )
        assert new_result.passed is True
        assert new_result.violations[0].approval_status is None

    def test_summary_reflects_approval_state(self):
        violations = (
            Violation(rule_id="r1", rule_description="d", severity=Severity.BLOCK, message="m"),
            Violation(rule_id="r2", rule_description="d", severity=Severity.BLOCK, message="m"),
        )
        new_result = apply_approvals(
            _make_result(violations),
            approved_rule_ids=frozenset({"r1"}),
            reason="ok",
        )
        assert "1 block(s) approved" in new_result.summary
        assert "1 block(s) pending" in new_result.summary
