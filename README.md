# Mimir — Context Server

> *In Norse mythology, Mimir was the wisest being in all the Nine Realms — guardian of the Well of Wisdom beneath Yggdrasil, the World Tree. Odin sacrificed his eye for a single drink from that well. **Mimir** brings that same depth of knowledge to your codebase.*

[![PyPI](https://img.shields.io/pypi/v/mimir-context-server)](https://pypi.org/project/mimir-context-server/)
[![Python](https://img.shields.io/pypi/pyversions/mimir-context-server)](https://pypi.org/project/mimir-context-server/)
[![PyPI - Client](https://img.shields.io/pypi/v/mimir-server-client?label=mimir-server-client)](https://pypi.org/project/mimir-server-client/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Mimir** is an intelligent context engine that helps LLMs understand large, multi-repo codebases. Instead of dumping raw files into a prompt, Mimir builds a semantic code graph with real cross-file dependency edges, ranks nodes by relevance and recency, and assembles a minimal, connected, token-budget-aware context bundle — exactly what the model needs, nothing it doesn't.

---

## The Problem

When you ask Claude or GPT to help with a large codebase, you face a brutal choice:

- **Too little context** — the model hallucinates or misses related code
- **Too much context** — you burn tokens on irrelevant files and hit limits
- **Copy-paste** — fragile, manual, doesn't scale across repos

## The Solution

Mimir indexes your code into a hierarchical graph of repositories, files, classes, and functions. Cross-file dependencies — function calls, type references, inheritance hierarchies — are resolved into typed edges. At query time, a hybrid search finds seed nodes and a beam search assembles the tightest connected subgraph that answers your question — within your token budget.

---

## Key Features

- **Hierarchical beam search** — finds connected code paths, not isolated snippets
- **Cross-file symbol resolution** — automatically discovers `CALLS`, `USES_TYPE`, and `INHERITS` edges across files using tree-sitter AST analysis
- **Hybrid search** — combines semantic embeddings, BM25 keyword matching, and name/path scoring for precise retrieval
- **Live file watching** — monitors your repos for changes and re-indexes on every save
- **Query intent classification** — automatically detects query type (locate, trace, write, debug) and tunes retrieval parameters
- **Subgraph expansion** — automatically surfaces callers, callees, type definitions, and config references
- **Connectivity quality scoring** — nodes scored by edge density, embedding presence, and content completeness; gap detection identifies under-indexed areas
- **Temporal reranking** — recently and frequently changed code scores higher
- **Session deduplication** — exponential decay model tracks what the LLM remembers
- **Write-path context** — shows interfaces, sibling implementations, test files, and DI registrations before you edit
- **Impact analysis** — reverse-traces callers and transitive dependencies to show blast radius
- **Architectural guardrails** — validates AI-generated changes against structural rules (layer violations, cycles, coupling, blast radius, scope bans) before commit, with agent policy for bounded autonomy and audit logging
- **Backstage catalog integration** — auto-populates service catalogs from the code graph with dependency drift detection
- **Multi-repo** — single server spans multiple repositories with cross-repo edge detection
- **MCP server** — plug-and-play with Claude Desktop, Cursor, and any MCP-compatible IDE
- **HTTP API** — shared team server for enterprise environments
- **Docker-ready** — zero Python setup, embedding model pre-baked
- **100% offline** — local embeddings, no API keys required for indexing

---

## Quick Start

```bash
pip install mimir-context-server
cd /your/project
mimir init          # creates mimir.toml
mimir index         # builds the semantic code graph
mimir search "how does authentication work?"
mimir serve         # start MCP server for your IDE

# Architectural guardrails
mimir guardrail init                          # generate example rules + agent policy
git diff | mimir guardrail check --diff -     # validate changes before committing
```

---

## Installation

| Package | Install | Who needs it |
|---|---|---|
| `mimir-context-server` | `pipx install mimir-context-server` | **Server operators** — devs who index repos and run the server |
| `mimir-server-client` | `pipx install mimir-server-client` | **Client devs** — devs who query a remote server without repos locally |

```bash
# Server (full install)
pipx install mimir-context-server

# Client only (lightweight)
pipx install mimir-server-client

# From source
git clone https://github.com/repfly/mimir && cd mimir
pip install -e .
```

---

## Documentation

| Topic | Description |
|---|---|
| [Configuration](docs/configuration.md) | `mimir.toml` reference with all sections and keys |
| [How It Works](docs/how-it-works.md) | Indexing pipeline, retrieval pipeline, session dedup, incremental indexing, live watching, quality scoring |
| [Serving Modes](docs/serving-modes.md) | Local MCP, shared HTTP server, remote proxy, MCP tools, HTTP API reference |
| [Docker Deployment](docs/docker.md) | Build, run, Compose, entrypoint modes, environment variables |
| [Workspaces](docs/workspaces.md) | Per-project isolation and workspace management |
| [Web Inspector](docs/web-inspector.md) | Browser-based graph visualization |
| [Backstage Integration](docs/backstage.md) | Auto-discovered service catalog and dependency drift detection |
| [Architectural Guardrails](docs/guardrails.md) | Rule types, agent policy, enforcement points, audit logging |
| [CLI Reference](docs/cli-reference.md) | Full command reference for `mimir` and `mimir-client` |
| [Architecture](docs/architecture.md) | Hexagonal architecture, project structure, data storage, supported languages |
| [Contributing](docs/contributing.md) | Development setup, testing, PyPI publishing |

---

## License

MIT
