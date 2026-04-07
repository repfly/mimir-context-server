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
from mimir.domain.guardrails_config import load_approval_config, load_rules


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
        assert "approval_status" not in d  # not set

    def test_to_dict_with_approval_status(self):
        v = Violation(
            rule_id="r1", rule_description="d", severity=Severity.BLOCK,
            message="m", approval_status="approved",
        )
        d = v.to_dict()
        assert d["approval_status"] == "approved"


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

    def test_pending_approvals(self):
        result = GuardrailResult(
            violations=(), passed=False, summary="pending",
            change_set=ChangeSet(), rules_evaluated=1,
            pending_approvals=("protect-container",),
        )
        assert result.has_pending_blocks is True
        d = result.to_dict()
        assert d["pending_approvals"] == ["protect-container"]

    def test_no_pending_approvals_not_in_dict(self):
        result = GuardrailResult(
            violations=(), passed=True, summary="ok",
            change_set=ChangeSet(), rules_evaluated=1,
        )
        assert result.has_pending_blocks is False
        assert "pending_approvals" not in result.to_dict()

    def test_format_for_llm_approval_status(self):
        result = GuardrailResult(
            violations=(
                Violation(
                    rule_id="r1", rule_description="d", severity=Severity.BLOCK,
                    message="m", approval_status="approved",
                ),
                Violation(
                    rule_id="r2", rule_description="d", severity=Severity.BLOCK,
                    message="m", approval_status="pending",
                ),
            ),
            passed=False, summary="fail",
            change_set=ChangeSet(), rules_evaluated=2,
            pending_approvals=("r2",),
        )
        text = result.format_for_llm()
        assert "[BLOCK - APPROVED]" in text
        assert "[BLOCK - PENDING]" in text


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


class TestLoadApprovalConfig:
    def test_returns_defaults_when_missing(self, tmp_path: Path):
        cfg = load_approval_config(tmp_path / "nonexistent.yaml")
        assert cfg.default_ttl_days == 7
        assert cfg.approvers == ()

    def test_returns_defaults_when_no_section(self, tmp_path: Path):
        f = tmp_path / "rules.yaml"
        f.write_text("rules: []\n")
        cfg = load_approval_config(f)
        assert cfg.default_ttl_days == 7

    def test_parses_approval_config(self, tmp_path: Path):
        f = tmp_path / "rules.yaml"
        f.write_text(textwrap.dedent("""\
            approval_config:
              default_ttl_days: 14
              approvers:
                - alice
                - bob
              approvals_dir: custom/approvals
            rules: []
        """))
        cfg = load_approval_config(f)
        assert cfg.default_ttl_days == 14
        assert cfg.approvers == ("alice", "bob")
        assert cfg.approvals_dir == "custom/approvals"

    def test_invalid_ttl_raises(self, tmp_path: Path):
        f = tmp_path / "rules.yaml"
        f.write_text(textwrap.dedent("""\
            approval_config:
              default_ttl_days: -1
            rules: []
        """))
        from mimir.domain.errors import RuleConfigError
        with pytest.raises(RuleConfigError, match="positive integer"):
            load_approval_config(f)
