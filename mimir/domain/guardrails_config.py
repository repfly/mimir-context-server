"""YAML configuration loader for guardrail rules and agent policies.

Fail-closed: any parse or validation error raises RuleConfigError immediately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from mimir.domain.errors import RuleConfigError
from mimir.domain.guardrails import Rule, RuleType, Severity


# ---------------------------------------------------------------------------
# Required config keys per rule type
# ---------------------------------------------------------------------------

_REQUIRED_CONFIG: dict[RuleType, set[str]] = {
    RuleType.DEPENDENCY_BAN: {"source_pattern", "target_pattern"},
    RuleType.CYCLE_DETECTION: {"scope"},
    RuleType.METRIC_THRESHOLD: {"metric", "threshold"},
    RuleType.IMPACT_THRESHOLD: {"max_impact"},
    RuleType.FILE_SCOPE_BAN: {"path_pattern"},
}

_VALID_METRICS = {"afferent_coupling", "efferent_coupling", "instability"}
_VALID_SCOPES = {"cross_repo", "intra_repo"}


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def load_rules(path: Path) -> list[Rule]:
    """Parse a mimir-rules.yaml file into Rule objects.

    Raises
    ------
    RuleConfigError
        On any parse or validation failure (fail-closed).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RuleConfigError(f"Rules file not found: {path}")
    except OSError as exc:
        raise RuleConfigError(f"Cannot read rules file {path}: {exc}")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"Invalid YAML in {path}: {exc}")

    if not isinstance(data, dict) or "rules" not in data:
        raise RuleConfigError(
            f"Rules file {path} must contain a top-level 'rules' key"
        )

    raw_rules = data["rules"]
    if not isinstance(raw_rules, list):
        raise RuleConfigError(f"'rules' must be a list in {path}")

    rules: list[Rule] = []
    for i, raw in enumerate(raw_rules):
        rules.append(_parse_rule(raw, index=i, path=path))
    return rules


def _parse_rule(raw: dict[str, Any], *, index: int, path: Path) -> Rule:
    """Parse and validate a single rule entry."""
    ctx = f"rule #{index} in {path}"

    if not isinstance(raw, dict):
        raise RuleConfigError(f"{ctx}: expected a mapping, got {type(raw).__name__}")

    # Required top-level fields
    for key in ("id", "type", "description", "severity"):
        if key not in raw:
            raise RuleConfigError(f"{ctx}: missing required field '{key}'")

    # Parse enums
    try:
        rule_type = RuleType(raw["type"])
    except ValueError:
        valid = ", ".join(rt.value for rt in RuleType)
        raise RuleConfigError(
            f"{ctx}: invalid type '{raw['type']}' — valid types: {valid}"
        )

    try:
        severity = Severity(raw["severity"])
    except ValueError:
        valid = ", ".join(s.value for s in Severity)
        raise RuleConfigError(
            f"{ctx}: invalid severity '{raw['severity']}' — valid: {valid}"
        )

    # Parse config
    config = raw.get("config", {})
    if not isinstance(config, dict):
        raise RuleConfigError(f"{ctx}: 'config' must be a mapping")

    # Validate required config keys for this rule type
    required = _REQUIRED_CONFIG.get(rule_type, set())
    missing = required - set(config.keys())
    if missing:
        raise RuleConfigError(
            f"{ctx}: missing required config keys for {rule_type.value}: "
            f"{', '.join(sorted(missing))}"
        )

    # Type-specific validation
    if rule_type == RuleType.METRIC_THRESHOLD:
        metric = config.get("metric")
        if metric not in _VALID_METRICS:
            raise RuleConfigError(
                f"{ctx}: invalid metric '{metric}' — valid: "
                f"{', '.join(sorted(_VALID_METRICS))}"
            )
        threshold = config.get("threshold")
        if not isinstance(threshold, (int, float)):
            raise RuleConfigError(f"{ctx}: 'threshold' must be a number")

    if rule_type == RuleType.CYCLE_DETECTION:
        scope = config.get("scope")
        if scope not in _VALID_SCOPES:
            raise RuleConfigError(
                f"{ctx}: invalid scope '{scope}' — valid: "
                f"{', '.join(sorted(_VALID_SCOPES))}"
            )

    if rule_type == RuleType.IMPACT_THRESHOLD:
        max_impact = config.get("max_impact")
        if not isinstance(max_impact, int) or max_impact < 0:
            raise RuleConfigError(f"{ctx}: 'max_impact' must be a non-negative integer")

    return Rule(
        id=str(raw["id"]),
        type=rule_type,
        description=str(raw["description"]),
        severity=severity,
        config=config,
    )


# ---------------------------------------------------------------------------
# Agent policy loading
# ---------------------------------------------------------------------------

def load_agent_policy(path: Path) -> list[dict[str, Any]]:
    """Parse a mimir-agent-policy.yaml file into policy dicts.

    Returns raw policy dicts; the AgentPolicy dataclass is defined in the
    agent_policy service module to keep the domain layer thin.

    Raises
    ------
    RuleConfigError
        On any parse or validation failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RuleConfigError(f"Agent policy file not found: {path}")
    except OSError as exc:
        raise RuleConfigError(f"Cannot read agent policy file {path}: {exc}")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"Invalid YAML in {path}: {exc}")

    if not isinstance(data, dict) or "policies" not in data:
        raise RuleConfigError(
            f"Agent policy file {path} must contain a top-level 'policies' key"
        )

    policies = data["policies"]
    if not isinstance(policies, list):
        raise RuleConfigError(f"'policies' must be a list in {path}")

    for i, p in enumerate(policies):
        if not isinstance(p, dict):
            raise RuleConfigError(
                f"Policy #{i} in {path}: expected a mapping"
            )
        if "name" not in p:
            raise RuleConfigError(f"Policy #{i} in {path}: missing 'name'")

    return policies
