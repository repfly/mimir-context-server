"""Tests for the AgentPolicyService."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mimir.domain.graph import CodeGraph
from mimir.domain.guardrails import ChangeSet
from mimir.domain.models import Edge, EdgeKind, Node, NodeKind
from mimir.services.agent_policy import AgentPolicy, AgentPolicyService, ReviewCondition
from mimir.services.impact import ImpactResult, ImpactService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_policy(**overrides) -> AgentPolicy:
    defaults = dict(
        name="test-agent",
        allow_patterns=("src/**", "tests/**"),
        deny_patterns=("src/auth/**", "src/billing/**"),
        require_review_conditions=(),
    )
    defaults.update(overrides)
    return AgentPolicy(**defaults)


def _make_graph() -> CodeGraph:
    graph = CodeGraph()
    graph.add_node(Node(
        id="repo:src/api.py::handle", repo="repo",
        kind=NodeKind.API_ENDPOINT, name="handle",
        path="src/api.py", start_line=5, end_line=15,
    ))
    graph.add_node(Node(
        id="repo:src/service.py::process", repo="repo",
        kind=NodeKind.FUNCTION, name="process",
        path="src/service.py", start_line=10, end_line=30,
    ))
    return graph


# ---------------------------------------------------------------------------
# File access tests
# ---------------------------------------------------------------------------


class TestFileAccess:
    def test_allowed_file(self):
        policy = _make_policy()
        svc = AgentPolicyService(impact_service=MagicMock())
        assert svc.check_file_access(policy, "src/models/user.py") is True

    def test_denied_file(self):
        policy = _make_policy()
        svc = AgentPolicyService(impact_service=MagicMock())
        assert svc.check_file_access(policy, "src/auth/login.py") is False

    def test_deny_takes_precedence(self):
        # Even though src/** allows, src/auth/** denies
        policy = _make_policy()
        svc = AgentPolicyService(impact_service=MagicMock())
        assert svc.check_file_access(policy, "src/auth/middleware.py") is False

    def test_no_match_denied_when_allow_exists(self):
        # File matches neither allow nor deny, but allow patterns exist
        policy = _make_policy()
        svc = AgentPolicyService(impact_service=MagicMock())
        assert svc.check_file_access(policy, "infrastructure/terraform.tf") is False

    def test_no_patterns_allows_all(self):
        policy = _make_policy(allow_patterns=(), deny_patterns=())
        svc = AgentPolicyService(impact_service=MagicMock())
        assert svc.check_file_access(policy, "anything/goes.py") is True

    def test_tests_allowed(self):
        policy = _make_policy()
        svc = AgentPolicyService(impact_service=MagicMock())
        assert svc.check_file_access(policy, "tests/test_auth.py") is True


# ---------------------------------------------------------------------------
# Review required tests
# ---------------------------------------------------------------------------


class TestReviewRequired:
    def test_impact_count_triggers(self):
        impact = MagicMock(spec=ImpactService)
        node = Node(id="n1", repo="r", kind=NodeKind.FUNCTION, name="f")
        impact.analyze.return_value = ImpactResult(
            target=node, direct_callers=[], type_users=[],
            implementors=[], test_files=[], transitive={},
            total_impact_count=20,
        )
        policy = _make_policy(require_review_conditions=(
            ReviewCondition(type="impact_count", threshold=10),
        ))
        svc = AgentPolicyService(impact_service=impact)
        graph = _make_graph()
        change = ChangeSet(modified_nodes=("repo:src/api.py::handle",))

        reasons = svc.check_review_required(policy, graph, change)
        assert len(reasons) == 1
        assert "Impact count" in reasons[0]

    def test_cross_repo_triggers(self):
        svc = AgentPolicyService(impact_service=MagicMock())
        policy = _make_policy(require_review_conditions=(
            ReviewCondition(type="cross_repo"),
        ))
        graph = _make_graph()
        change = ChangeSet(new_edges=(
            Edge(source="a:f", target="b:g", kind=EdgeKind.API_CALLS),
        ))

        reasons = svc.check_review_required(policy, graph, change)
        assert len(reasons) == 1
        assert "cross-repo" in reasons[0]

    def test_modifies_api_triggers(self):
        svc = AgentPolicyService(impact_service=MagicMock())
        policy = _make_policy(require_review_conditions=(
            ReviewCondition(type="modifies_api"),
        ))
        graph = _make_graph()
        change = ChangeSet(modified_nodes=("repo:src/api.py::handle",))

        reasons = svc.check_review_required(policy, graph, change)
        assert len(reasons) == 1
        assert "API endpoint" in reasons[0]

    def test_no_conditions_no_review(self):
        svc = AgentPolicyService(impact_service=MagicMock())
        policy = _make_policy(require_review_conditions=())
        graph = _make_graph()
        change = ChangeSet(modified_nodes=("repo:src/service.py::process",))

        reasons = svc.check_review_required(policy, graph, change)
        assert reasons == []


# ---------------------------------------------------------------------------
# from_dict tests
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_parse_policy(self):
        data = {
            "name": "my-agent",
            "allow": ["src/**"],
            "deny": ["src/auth/**"],
            "require_review_when": [
                {"type": "impact_count", "threshold": 15},
                {"type": "cross_repo"},
            ],
        }
        policy = AgentPolicy.from_dict(data)
        assert policy.name == "my-agent"
        assert policy.allow_patterns == ("src/**",)
        assert policy.deny_patterns == ("src/auth/**",)
        assert len(policy.require_review_conditions) == 2

    def test_minimal_policy(self):
        data = {"name": "minimal"}
        policy = AgentPolicy.from_dict(data)
        assert policy.name == "minimal"
        assert policy.allow_patterns == ()
        assert policy.deny_patterns == ()
