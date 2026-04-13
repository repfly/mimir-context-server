# Architecture & Project Structure

> [Back to README](../README.md)

## Hexagonal Architecture

Mimir follows a hexagonal (ports & adapters) architecture with constructor-based dependency injection. No singletons or module-level state.

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

## Project Structure

```
├── mimir/                      # Server package (mimir-context-server)
│   ├── domain/                 # Core models, config, graph, catalog, guardrails, errors
│   ├── ports/                  # Interface definitions (embedder, parser, stores)
│   ├── services/               # Business logic (indexing, retrieval, catalog, impact, guardrail, temporal, session, quality, watcher)
│   ├── infra/                  # Implementations (tree-sitter, embedders, SQLite, ChromaDB)
│   ├── adapters/               # External interfaces (CLI, MCP, HTTP, web UI)
│   └── container.py            # Dependency injection wiring
├── backstage-plugin/           # Backstage catalog backend module
│   └── plugins/catalog-backend-module-mimir/
├── client/                     # Client package (mimir-server-client)
│   └── mimir_client/           # Lightweight MCP proxy + health check CLI
├── docs/                       # Documentation
├── tests/                      # Test suite
├── Dockerfile                  # Server container with pre-baked embedding model
├── docker-compose.yml          # Docker Compose for team deployment
├── docker-entrypoint.sh        # Entrypoint: auto index-then-serve
├── pyproject.toml              # Server package metadata
├── mimir.toml                  # Example configuration
├── mimir-rules.yaml            # Example architectural guardrail rules
└── mimir-agent-policy.yaml     # Example AI agent scope policies
```

## Data Storage

Mimir stores all index data in a local directory (default `.mimir/`, configurable via `data_dir` in config). It is split into two subfolders so git can track one and ignore the other:

```
.mimir/
├── project/                    # tracked in git — shared with CI & teammates
│   └── graph.db                #   SQLite: nodes, edges, repo_state, embeddings
└── session/                    # ignored by git — personal / re-derivable
    ├── sessions.db             #   SQLite: session state for deduplication
    ├── models/                 #   downloaded embedding weights
    ├── chroma/                 #   ChromaDB data (only if backend = "chroma")
    └── guardrail_audit.jsonl   #   Guardrail check audit log (if enabled)
```

Committing `.mimir/project/graph.db` lets CI run `mimir guardrail check` without rebuilding the graph from scratch, which is the biggest cold-start cost in the pipeline.

## Supported Languages

Mimir uses tree-sitter grammars for language-agnostic symbol extraction and cross-file resolution. Currently supported:

Python, TypeScript, JavaScript, Go, Java, Rust, C, C++, Ruby, Swift, Kotlin, C#, TOML, YAML, JSON

See also: [How It Works](how-it-works.md), [Contributing](contributing.md).
