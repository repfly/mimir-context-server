"""Agent policy system — bounded autonomy for AI coding agents.

Defines what files/areas an AI agent is allowed to modify and conditions
that trigger mandatory human review.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Optional

from mimir.domain.graph import CodeGraph
from mimir.domain.guardrails import ChangeSet
from mimir.domain.models import NodeKind
from mimir.services.impact import ImpactService


# ---------------------------------------------------------------------------
# Domain types (kept here to avoid circular imports with guardrails_config)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewCondition:
    """Condition that triggers human review requirement."""

    type: str  # "impact_count", "cross_repo", "modifies_api"
    threshold: Any = None


@dataclass(frozen=True)
class AgentPolicy:
    """Defines what an AI agent is allowed to modify."""

    name: str
    allow_patterns: tuple[str, ...] = ()
    deny_patterns: tuple[str, ...] = ()
    require_review_conditions: tuple[ReviewCondition, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentPolicy:
        """Create from a parsed YAML policy dict."""
        conditions: list[ReviewCondition] = []
        for rc in data.get("require_review_when", []):
            conditions.append(ReviewCondition(
                type=rc.get("type", ""),
                threshold=rc.get("threshold"),
            ))
        return cls(
            name=data.get("name", "unnamed"),
            allow_patterns=tuple(data.get("allow", ())),
            deny_patterns=tuple(data.get("deny", ())),
            require_review_conditions=tuple(conditions),
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AgentPolicyService:
    """Evaluates agent policies for file access and review requirements."""

    def __init__(self, impact_service: ImpactService) -> None:
        self._impact = impact_service

    def check_file_access(self, policy: AgentPolicy, file_path: str) -> bool:
        """Return True if the agent is allowed to modify this file.

        Deny patterns take precedence over allow patterns.
        """
        # Check deny first (takes precedence)
        for pattern in policy.deny_patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return False

        # If allow patterns exist, file must match at least one
        if policy.allow_patterns:
            return any(
                fnmatch.fnmatch(file_path, p) for p in policy.allow_patterns
            )

        # No allow/deny patterns → allowed by default
        return True

    def check_review_required(
        self,
        policy: AgentPolicy,
        graph: CodeGraph,
        change: ChangeSet,
    ) -> list[str]:
        """Return list of reasons human review is required, or empty list."""
        reasons: list[str] = []

        for condition in policy.require_review_conditions:
            if condition.type == "impact_count":
                threshold = condition.threshold or 15
                for node_id in change.modified_nodes:
                    result = self._impact.analyze(graph, node_id=node_id)
                    if result and result.total_impact_count > threshold:
                        reasons.append(
                            f"Impact count {result.total_impact_count} exceeds "
                            f"threshold {threshold} for {node_id}"
                        )
                        break

            elif condition.type == "cross_repo":
                for edge in change.new_edges:
                    if edge.is_cross_repo:
                        reasons.append("Change introduces cross-repo dependencies")
                        break

            elif condition.type == "modifies_api":
                for node_id in change.modified_nodes:
                    node = graph.get_node(node_id)
                    if node and node.kind == NodeKind.API_ENDPOINT:
                        reasons.append(f"Modifies API endpoint: {node.name}")
                        break

        return reasons
