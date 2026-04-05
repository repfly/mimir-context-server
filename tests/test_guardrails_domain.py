"""Tests for guardrails domain models and YAML config loading."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mimir.domain.errors import RuleConfigError
from mimir.domain.guardrails import (
    ChangeSet,
    GuardrailResult,
    Rule,
    RuleType,
    Severity,
    Violation,
)
from mimir.domain.guardrails_config import load_rules


# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


class TestRuleType:
    def test_all_values(self):
        assert len(RuleType) == 5
        assert RuleType("dependency_ban") == RuleType.DEPENDENCY_BAN
        assert RuleType("cycle_detection") == RuleType.CYCLE_DETECTION
        assert RuleType("metric_threshold") == RuleType.METRIC_THRESHOLD
        assert RuleType("impact_threshold") == RuleType.IMPACT_THRESHOLD
        assert RuleType("file_scope_ban") == RuleType.FILE_SCOPE_BAN


class TestSeverity:
    def test_all_values(self):
        assert len(Severity) == 3
        assert Severity("warning") == Severity.WARNING
        assert Severity("error") == Severity.ERROR
        assert Severity("block") == Severity.BLOCK


class TestRule:
    def test_create_rule(self):
        rule = Rule(
            id="test-rule",
            type=RuleType.DEPENDENCY_BAN,
            description="Test rule",
            severity=Severity.ERROR,
            config={"source_pattern": "*/domain/**", "target_pattern": "*/infra/**"},
        )
        assert rule.id == "test-rule"
        assert rule.type == RuleType.DEPENDENCY_BAN
        assert rule.severity == Severity.ERROR

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            Rule(id="", type=RuleType.DEPENDENCY_BAN, description="x", severity=Severity.ERROR)

    def test_frozen(self):
        rule = Rule(id="r", type=RuleType.DEPENDENCY_BAN, description="x", severity=Severity.ERROR)
        with pytest.raises(AttributeError):
            rule.id = "new"  # type: ignore[misc]


class TestChangeSet:
    def test_empty(self):
        cs = ChangeSet()
        assert cs.modified_nodes == ()
        assert cs.new_edges == ()
        assert cs.affected_files == ()

    def test_frozen(self):
        cs = ChangeSet(affected_files=("a.py",))
        with pytest.raises(AttributeError):
            cs.affected_files = ()  # type: ignore[misc]


class TestViolation:
    def test_to_dict(self):
        v = Violation(
            rule_id="r1",
            rule_description="desc",
            severity=Severity.ERROR,
            message="bad import",
            evidence=("edge: A -> B",),
            file_path="src/a.py",
            suggested_fix="Use a port instead",
        )
        d = v.to_dict()
        assert d["rule_id"] == "r1"
        assert d["severity"] == "error"
        assert d["evidence"] == ["edge: A -> B"]


class TestGuardrailResult:
    def test_passed_no_violations(self):
        result = GuardrailResult(
            violations=(),
            passed=True,
            summary="All checks passed",
            change_set=ChangeSet(),
            rules_evaluated=3,
        )
        assert result.passed is True
        assert result.rules_evaluated == 3

    def test_to_dict(self):
        result = GuardrailResult(
            violations=(
                Violation(
                    rule_id="r1", rule_description="d", severity=Severity.ERROR,
                    message="m",
                ),
            ),
            passed=False,
            summary="1 violation",
            change_set=ChangeSet(affected_files=("a.py",)),
            rules_evaluated=1,
        )
        d = result.to_dict()
        assert d["passed"] is False
        assert len(d["violations"]) == 1
        assert d["affected_files"] == ["a.py"]

    def test_format_for_llm_passed(self):
        result = GuardrailResult(
            violations=(), passed=True, summary="ok",
            change_set=ChangeSet(), rules_evaluated=2,
        )
        text = result.format_for_llm()
        assert "PASSED" in text
        assert "No architectural violations" in text

    def test_format_for_llm_failed(self):
        result = GuardrailResult(
            violations=(
                Violation(
                    rule_id="r1", rule_description="desc", severity=Severity.BLOCK,
                    message="blocked", evidence=("e1",), file_path="x.py",
                    suggested_fix="fix it",
                ),
            ),
            passed=False, summary="fail",
            change_set=ChangeSet(), rules_evaluated=1,
        )
        text = result.format_for_llm()
        assert "FAILED" in text
        assert "[BLOCK]" in text
        assert "fix it" in text


# ---------------------------------------------------------------------------
# YAML config loading tests
# ---------------------------------------------------------------------------


class TestLoadRules:
    def test_load_valid_rules(self, tmp_path: Path):
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(textwrap.dedent("""\
            rules:
              - id: r1
                type: dependency_ban
                description: "No domain to infra"
                severity: error
                config:
                  source_pattern: "*/domain/**"
                  target_pattern: "*/infra/**"
              - id: r2
                type: cycle_detection
                description: "No cycles"
                severity: warning
                config:
                  scope: cross_repo
        """))
        rules = load_rules(rules_file)
        assert len(rules) == 2
        assert rules[0].id == "r1"
        assert rules[0].type == RuleType.DEPENDENCY_BAN
        assert rules[1].type == RuleType.CYCLE_DETECTION

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(RuleConfigError, match="not found"):
            load_rules(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        f.write_text("rules:\n  - [invalid yaml structure")
        with pytest.raises(RuleConfigError):
            load_rules(f)

    def test_missing_rules_key(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        f.write_text("other_key: true\n")
        with pytest.raises(RuleConfigError, match="top-level 'rules' key"):
            load_rules(f)

    def test_invalid_rule_type(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        f.write_text(textwrap.dedent("""\
            rules:
              - id: r1
                type: nonexistent_type
                description: x
                severity: error
        """))
        with pytest.raises(RuleConfigError, match="invalid type"):
            load_rules(f)

    def test_missing_config_keys(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        f.write_text(textwrap.dedent("""\
            rules:
              - id: r1
                type: dependency_ban
                description: x
                severity: error
                config:
                  source_pattern: "**"
        """))
        with pytest.raises(RuleConfigError, match="missing required config keys"):
            load_rules(f)

    def test_invalid_metric(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        f.write_text(textwrap.dedent("""\
            rules:
              - id: r1
                type: metric_threshold
                description: x
                severity: warning
                config:
                  metric: nonexistent_metric
                  threshold: 10
        """))
        with pytest.raises(RuleConfigError, match="invalid metric"):
            load_rules(f)

    def test_invalid_scope(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        f.write_text(textwrap.dedent("""\
            rules:
              - id: r1
                type: cycle_detection
                description: x
                severity: warning
                config:
                  scope: invalid_scope
        """))
        with pytest.raises(RuleConfigError, match="invalid scope"):
            load_rules(f)

    def test_load_example_rules_file(self):
        """Ensure the shipped mimir-rules.yaml parses successfully."""
        rules_path = Path(__file__).parent.parent / "mimir-rules.yaml"
        if rules_path.exists():
            rules = load_rules(rules_path)
            assert len(rules) > 0
