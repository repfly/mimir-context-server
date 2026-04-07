# Architectural Guardrails

> [Back to README](../README.md)

AI coding agents generate syntactically correct code that can silently introduce circular dependencies, layer violations, or high-impact API changes. Mimir Guardrails validates changes against structural rules **before code is committed**, using the code graph no other tool has.

## Rule Types

| Rule Type | What It Detects |
|---|---|
| `dependency_ban` | Forbidden imports between layers (e.g., domain → infra) |
| `cycle_detection` | Circular dependencies introduced by new edges (cross-repo or intra-repo) |
| `metric_threshold` | Coupling thresholds exceeded (afferent, efferent, instability) |
| `impact_threshold` | Blast radius too large (e.g., changing a function affects 50+ consumers) |
| `file_scope_ban` | Protected files/directories that require human review |

## Quick Start

```bash
# Generate example rules + agent policy files
mimir guardrail init

# Auto-detect changes from git (staged → unstaged → branch diff)
mimir guardrail check

# Diff against a specific base branch
mimir guardrail check --base main

# Explicit diff via stdin (for CI)
git diff main...HEAD | mimir guardrail check --diff -

# JSON output for CI pipelines
mimir guardrail check --base main --output json

# GitHub PR comment output
mimir guardrail check --base main --output github-pr-comment

# Dry-run: validate rules syntax against current graph
mimir guardrail test
```

## Rules Configuration (`mimir-rules.yaml`)

```yaml
rules:
  - id: no-domain-to-infra
    type: dependency_ban
    description: "Domain layer must not import from infrastructure"
    severity: error
    config:
      source_pattern: "*/domain/**"
      target_pattern: "*/infra/**"

  - id: no-circular-services
    type: cycle_detection
    description: "No circular dependencies between services"
    severity: error
    config:
      scope: cross_repo
      edge_kinds: [api_calls, shared_lib]

  - id: max-inbound-coupling
    type: metric_threshold
    description: "No module should have more than 20 inbound dependencies"
    severity: warning
    config:
      metric: afferent_coupling
      threshold: 20

  - id: api-change-blast-radius
    type: impact_threshold
    description: "Public API changes must not affect more than 15 consumers"
    severity: error
    config:
      target_kind: [api_endpoint]
      max_impact: 15
      max_hops: 3

  - id: protect-auth
    type: file_scope_ban
    description: "Auth module requires human review"
    severity: block
    config:
      path_pattern: "*/auth/**"
      require_human_approval: true
```

Severity levels: `warning` (report only), `error` (block commit), `block` (require human approval).

## Approval Workflow

When a `block` severity rule fires, the pipeline exits with code **2** (pending approval) instead of code 1 (error). Approvals are **git-native YAML files** stored in `.mimir/approvals/` — platform-agnostic, auditable via `git log`.

### Configuration

Add an `approval_config` section to `mimir-rules.yaml`:

```yaml
approval_config:
  default_ttl_days: 7           # approval expires after 7 days
  approvers: [alice, bob]       # authorized approvers (git user.name)
  approvals_dir: ".mimir/approvals"
```

Per-rule overrides are supported in `file_scope_ban` configs:

```yaml
- id: protect-auth
  type: file_scope_ban
  severity: block
  config:
    path_pattern: "*/auth/**"
    require_human_approval: true
    approvers: [security-lead]   # per-rule override
    ttl_days: 3                  # per-rule TTL
```

### Workflow

```bash
# 1. CI or local check finds BLOCK violations
mimir guardrail check --base main
# → exit code 2 (pending approval)
# → Reports: rule IDs and instructions

# 2. Developer creates approval request locally
mimir guardrail request --rules protect-auth

# 3. Reviewer approves
mimir guardrail approve apr-a1b2c3d4 --reason "Reviewed auth changes"

# 4. Commit and push the approval file
git add .mimir/approvals/apr-a1b2c3d4.yaml
git commit -m "Add guardrail approval for auth changes"
git push

# 5. CI re-runs → approval matches → passes (exit code 0)
```

Approvals are matched by **rule ID + branch name** — no fragile diff-hash binding. The `diff_hash` is still stored in the YAML file for audit purposes.

### CLI Commands

| Command | Description |
|---|---|
| `mimir guardrail request --rules <ids>` | Create an approval request for BLOCK violations |
| `mimir guardrail approve <id> --reason "..."` | Grant approval |
| `mimir guardrail revoke <id>` | Revoke an approval |
| `mimir guardrail status` | List all approval requests |
| `mimir guardrail clean` | Remove expired/revoked approvals |

### Key Properties

- **Branch-scoped matching**: Approvals are matched by rule ID + branch name, making them stable across environments.
- **TTL expiry**: Approvals expire after a configurable number of days (default 7).
- **Exit codes**: `0` = passed, `1` = errors, `2` = blocks pending approval.
- **CI behavior**: Pending approvals emit a warning but don't fail CI by default. Set `MIMIR_FAIL_ON_PENDING=true` to enforce.
- **`--no-approvals` flag**: Skip approval matching on `guardrail check` to see raw violations.

## Agent Policy (`mimir-agent-policy.yaml`)

Restrict what AI agents can modify — implementing the **bounded autonomy** pattern:

```yaml
policies:
  - name: default-agent
    allow:
      - "src/**"
      - "tests/**"
    deny:
      - "src/auth/**"
      - "src/billing/**"
      - "infrastructure/**"
    require_review_when:
      - type: impact_count
        threshold: 15
      - type: cross_repo
      - type: modifies_api
```

AI agents call the `can_i_modify` MCP tool to check access before editing files.

## Enforcement Points

| Point | How |
|---|---|
| **MCP (real-time)** | AI agent calls `validate_change` tool before committing |
| **Pre-commit hook** | `cp mimir/adapters/hooks/pre_commit.sh .git/hooks/pre-commit` |
| **CI/CD** | GitHub Action at `mimir/adapters/ci/guardrails-action.yml` |
| **HTTP API** | `POST /api/v1/guardrails/check` for custom integrations |

## Audit Logging

Every guardrail check can be logged for compliance evidence (EU AI Act Article 9):

```
.mimir/guardrail_audit.jsonl
```

Each entry includes: timestamp, change hash, rules evaluated, violations found, agent identity, and pass/fail status.

See also: [Serving Modes](serving-modes.md) for the HTTP guardrail endpoint, [CLI Reference](cli-reference.md) for `mimir guardrail` subcommands.
