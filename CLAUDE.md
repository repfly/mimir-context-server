# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Mimir Context Server** — an intelligent code context engine for LLMs. It builds a semantic code graph from source repos, ranks nodes by relevance/recency, and assembles minimal, token-budget-aware context bundles. Python 3.11+, MIT licensed.

## Common Commands

```bash
# Install for development
pip install -e ".[dev]"

# Run tests (the venv lives at ./.venv — activate it or call it directly)
.venv/bin/pytest
# or: source .venv/bin/activate && pytest
.venv/bin/pytest tests/test_guardrail_service.py -k test_name   # Single test/module

# No linter or formatter is configured (no ruff/black/mypy). Tests are the only gate.
# pytest runs in asyncio_mode = "auto" — async tests need no @pytest.mark.asyncio.

# CLI usage
mimir init              # Create mimir.toml config
mimir index             # Build/update semantic code graph
mimir search "query"    # Search and assemble context
mimir serve             # Start MCP server (stdio)
mimir serve --http      # Start shared HTTP server (port 8421)
mimir quality           # Analyze graph connectivity gaps

# Guardrails
mimir guardrail init    # Generate example rules + agent policy
mimir guardrail check                                    # Auto-detect diff from git
mimir guardrail check --base main                        # Diff against specific branch
mimir guardrail check --diff - --rules mimir-rules.yaml  # Validate diff (stdin)
mimir guardrail test    # Dry-run: validate rule syntax

# Guardrail Approvals (HEAD-commit trailer model)
mimir guardrail approve <rule-ids...> --reason "..."    # Add Mimir-Approved trailer via empty commit
```

## Guardrail Approvals

BLOCK-severity rules require a human approval to pass. Approvals are stateless:
they live entirely in the HEAD commit's message as trailers. There is no
`.mimir/approvals/` directory, no registry, no TTL.

```
Mimir-Approved: protect-container, protect-ci
Mimir-Approved-Reason: legal signoff ticket #4821
```

Workflow:

1. `mimir guardrail check` (local run or pre-commit hook) fails on a BLOCK violation and lists the failing rule ids.
2. Someone runs `mimir guardrail approve <rule-ids> --reason "..."` on the
   PR branch. This creates an empty commit with the trailer.
3. Re-run the check; it reads HEAD trailers and clears the matching BLOCKs. (Repo CI does not run guardrails.)
4. Any subsequent commit without the trailer invalidates the approval because
   HEAD has moved. No `revoke` command is needed.

`apply_approvals()` in `mimir/services/guardrail.py` accepts the approval
whenever the rule id is listed in the trailer and `Mimir-Approved-Reason`
is non-empty. There is no self-approval guard — whoever commits the
trailer is trusted; the audit trail lives in `git log`.

## Architecture

Hexagonal (ports & adapters) with constructor-based dependency injection. No singletons or module-level state.

```
Adapters (CLI, MCP, HTTP, Web UI)
    ↓
Container (DI wiring — container.py)
    ↓
Services (business logic: indexing, retrieval, temporal, quality, intent, session, impact, guardrail, watcher)
    ↓
Domain (core models: CodeGraph, Node, Edge, Config, Session — all frozen dataclasses/enums)
    ↓
Ports (protocol interfaces: Parser, Embedder, VectorStore, GraphStore, SessionStore)
    ↓
Infra (concrete implementations: tree-sitter, ONNX Runtime embeddings (local) / Jina API, SQLite, in-memory NumPy vector index)
```

### Key Layers

- **`mimir/domain/`** — Immutable core: `models.py` (NodeKind/EdgeKind enums, Node/Edge), `graph.py` (NetworkX-backed CodeGraph), `config.py` (TOML-mapped dataclasses), `guardrails.py` (Rule/Violation/ChangeSet/GuardrailResult)
- **`mimir/ports/`** — Protocol interfaces for dependency injection boundaries
- **`mimir/services/`** — All business logic (19 modules). Core pipeline: `indexing.py` (parse→graph→embed), `retrieval.py` (query→seed→expand→rank→budget-fit), `summarizer.py`, `graph_linker.py` (cross-file reference resolution). Ranking signals: `intent.py` (query classification), `quality.py`, `temporal.py`. Guardrails: `guardrail.py` (rule engine), `guardrail_report.py`, `guardrail_trailers.py` (approval trailers), `diff_analyzer.py` (git diff→ChangeSet), `agent_policy.py` (bounded autonomy). Also `impact.py` (blast-radius reverse-tracing), `session.py` (dedup w/ exponential decay), `write_context.py` (write-path bundles), `catalog.py` (Backstage), `watcher.py` (live re-index)
- **`mimir/infra/`** — Pluggable implementations: `parsers/tree_sitter.py`, `embedders/local.py`+`jina.py`, `stores/sqlite_graph.py`+`sqlite_session.py`, `vector_stores/numpy_store.py` (in-memory, rebuilt from `graph.db` embeddings on startup)
- **`mimir/adapters/`** — External interfaces: `cli.py` (Typer entry point) + `cli_support/`, `mcp_server.py` (MCP stdio), `remote_mcp.py` (proxy to remote server), `http_server.py` (REST API), `web/server.py` (Web Inspector UI), plus `ci/` (GitHub Action) and `hooks/` (pre-commit) integration assets
- **`mimir/container.py`** — Wires all layers together via DI

### Retrieval Pipeline

1. Classify intent (locate/trace/write/debug) → 2. Embed query → 3. Hybrid search (semantic + BM25 + name/path) → 4. Hierarchical beam search → 5. Subgraph expansion via BFS along typed edges → 6. Quality + temporal reranking → 7. Budget fitting → 8. Topological ordering → ContextBundle

### Indexing Pipeline

1. Tree-sitter parse → 2. Build node/edge graph → 3. Cross-file reference resolution → 4. Heuristic summarization → 5. Embedding → 6. Persist to SQLite + vector store. Supports incremental indexing via git diff.

## CI

`.github/workflows/ci.yml` runs on PRs and pushes to `main`/`develop` and does only two things: `pytest --tb=short -q` on Python 3.11/3.12/3.13, and build verification (`python -m build` for the server and `client/` packages + `twine check --strict`). Guardrail checks are not run in CI; use `mimir guardrail check` locally or via the pre-commit hook.

`.github/workflows/publish.yml` releases on `v*` tag pushes only: tests → build → publish both packages to PyPI (trusted publishing, `pypi` environment) → GitHub Release with the dists attached. Versions come from the git tag via setuptools-scm.

## Configuration

Primary config: `mimir.toml` (TOML). Key sections: `[[repos]]`, `[indexing]`, `[embeddings]`, `[retrieval]`, `[temporal]`, `[session]`, `[watcher]`.

`mimir.toml` indexes the Mimir source itself (used for both development and CI).

Guardrails config: `mimir-rules.yaml` (architectural rules) and `mimir-agent-policy.yaml` (agent scope restrictions). Both use YAML format.

## Storage

Default location: `.mimir/`, split into two subfolders:

- **`.mimir/project/`** — tracked in git. Contains `graph.db` (the code graph). Committing this lets CI skip re-indexing and lets new developers get a working context engine on clone.
- **`.mimir/session/`** — ignored by git. Contains `sessions.db` (session state), `models/` (downloaded ONNX embedding models). Anything personal or re-derivable lives here.

Run `mimir index` and commit `.mimir/project/graph.db` after significant code changes.

## Docker

Pre-bakes `all-mpnet-base-v2` embedding model. Entry point modes: `auto` (index then serve), `serve`, `index`, or any `mimir` subcommand. Port 8421 for HTTP.

## Client Package

`client/mimir_client/` — lightweight MCP proxy for connecting to a remote Mimir HTTP server. Separate PyPI package: `mimir-server-client`.
