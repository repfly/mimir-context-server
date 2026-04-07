# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Mimir Context Server** — an intelligent code context engine for LLMs. It builds a semantic code graph from source repos, ranks nodes by relevance/recency, and assembles minimal, token-budget-aware context bundles. Python 3.11+, MIT licensed.

## Common Commands

```bash
# Install for development
pip install -e ".[dev]"

# Run tests
pytest

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

# Guardrail Approvals (exit code 2 = pending approval)
mimir guardrail request --rules <rule-ids> --diff -  # Create approval request
mimir guardrail approve <request-id> --reason "..."  # Grant approval
mimir guardrail revoke <request-id>                  # Revoke approval
mimir guardrail status                               # List approvals
mimir guardrail clean                                # Remove expired approvals
```

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
Ports (protocol interfaces: Parser, Embedder, VectorStore, GraphStore, SessionStore, LLMClient)
    ↓
Infra (concrete implementations: tree-sitter, sentence-transformers/jina, SQLite, ChromaDB, LiteLLM)
```

### Key Layers

- **`mimir/domain/`** — Immutable core: `models.py` (NodeKind/EdgeKind enums, Node/Edge), `graph.py` (NetworkX-backed CodeGraph), `config.py` (TOML-mapped dataclasses), `guardrails.py` (Rule/Violation/ChangeSet/GuardrailResult)
- **`mimir/ports/`** — Protocol interfaces for dependency injection boundaries
- **`mimir/services/`** — All business logic. Heaviest files: `indexing.py` (parse→graph→embed pipeline), `retrieval.py` (query→seed→expand→rank→budget-fit), `guardrail.py` (rule evaluation engine), `diff_analyzer.py` (git diff→ChangeSet), `agent_policy.py` (bounded autonomy)
- **`mimir/infra/`** — Pluggable implementations: `parsers/tree_sitter.py`, `embedders/local.py`+`jina.py`, `stores/sqlite_graph.py`, `vector_stores/numpy_store.py`+`chroma.py`
- **`mimir/adapters/`** — External interfaces: `cli.py` (Typer entry point), `mcp_server.py` (MCP stdio), `http_server.py` (REST API)
- **`mimir/container.py`** — Wires all layers together via DI

### Retrieval Pipeline

1. Classify intent (locate/trace/write/debug) → 2. Embed query → 3. Hybrid search (semantic + BM25 + name/path) → 4. Hierarchical beam search → 5. Subgraph expansion via BFS along typed edges → 6. Quality + temporal reranking → 7. Budget fitting → 8. Topological ordering → ContextBundle

### Indexing Pipeline

1. Tree-sitter parse → 2. Build node/edge graph → 3. Cross-file reference resolution → 4. Heuristic summarization → 5. Embedding → 6. Persist to SQLite + vector store. Supports incremental indexing via git diff.

## Configuration

Primary config: `mimir.toml` (TOML). Key sections: `[[repos]]`, `[indexing]`, `[embeddings]`, `[retrieval]`, `[temporal]`, `[session]`, `[vector_db]`, `[llm]`.

`mimir.toml` indexes the Mimir source itself (used for both development and CI).

Guardrails config: `mimir-rules.yaml` (architectural rules) and `mimir-agent-policy.yaml` (agent scope restrictions). Both use YAML format.

## Storage

Default location: `.mimir/` — contains `graph.db` (SQLite), `sessions.db`, and optionally `chroma/` for persistent vector store.

## Docker

Pre-bakes `all-mpnet-base-v2` embedding model. Entry point modes: `auto` (index then serve), `serve`, `index`, or any `mimir` subcommand. Port 8421 for HTTP.

## Client Package

`client/mimir_client/` — lightweight MCP proxy for connecting to a remote Mimir HTTP server. Separate PyPI package: `mimir-server-client`.
