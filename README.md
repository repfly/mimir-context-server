# Mimir — Context Server

> *In Norse mythology, Mimir was the wisest being in all the Nine Realms — guardian of the Well of Wisdom beneath Yggdrasil, the World Tree. Odin sacrificed his eye for a single drink from that well. **Mimir** brings that same depth of knowledge to your codebase.*

[![PyPI](https://img.shields.io/pypi/v/mimir-context-server)](https://pypi.org/project/mimir-context-server/)
[![Python](https://img.shields.io/pypi/pyversions/mimir-context-server)](https://pypi.org/project/mimir-context-server/)
[![PyPI - Client](https://img.shields.io/pypi/v/mimir-server-client?label=mimir-server-client)](https://pypi.org/project/mimir-server-client/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Mimir** is an intelligent context engine that helps LLMs understand large, multi-repo codebases. Instead of dumping raw files into a prompt, Mimir builds a semantic code graph, ranks nodes by relevance and recency, and assembles a minimal, connected, token-budget-aware context bundle — exactly what the model needs, nothing it doesn't.

---

## The Problem

When you ask Claude or GPT to help with a large codebase, you face a brutal choice:

- **Too little context** — the model hallucinates or misses related code
- **Too much context** — you burn tokens on irrelevant files and hit limits
- **Copy-paste** — fragile, manual, doesn't scale across repos

## The Solution

Mimir indexes your code into a hierarchical graph of repositories, files, classes, and functions. Every node is embedded semantically. At query time, a beam search traverses the graph to find the tightest connected subgraph that answers your question — within your token budget.

---

## Key Features

- **Hierarchical beam search** — finds connected code paths, not isolated snippets
- **Subgraph expansion** — automatically surfaces callers, callees, type definitions, and config references
- **Temporal reranking** — recently and frequently changed code scores higher
- **Session deduplication** — code already sent to the LLM is summarised or omitted on subsequent turns
- **Multi-repo** — single server spans multiple repositories with cross-repo edge detection
- **Workspace isolation** — per-project indexes, agents can't cross project boundaries
- **MCP server** — plug-and-play with Claude Desktop, Cursor, and any MCP-compatible IDE
- **HTTP API** — shared team server for enterprise environments where devs don't have all repos locally
- **Docker-ready** — zero Python setup, embedding model pre-baked, auto index-then-serve
- **100% offline** — local sentence-transformers embedding, no API keys required for indexing

---

## Quick Start

```bash
pip install mimir-context-server
cd /your/project
mimir init          # creates mimir.toml
mimir index         # builds the semantic code graph
mimir search "how does authentication work?"
mimir serve         # start MCP server for your IDE
```

---

## Installation

There are two packages — pick the one that matches your role:

| Package | Install | Who needs it |
|---|---|---|
| `mimir-context-server` | `pipx install mimir-context-server` | **Server operators** — devs who index repos and run the server (local or shared) |
| `mimir-server-client` | `pipx install mimir-server-client` | **Client devs** — devs who query a remote server without any repos locally |

### Server (full install)

```bash
# pipx (recommended — isolated global install, `mimir` on PATH)
pipx install mimir-context-server

# or pip
pip install mimir-context-server
```

### Client only (lightweight)

```bash
pipx install mimir-server-client
```

### Docker (server, zero Python setup)

```bash
docker build -t mimir-server .
```

### From source

```bash
git clone https://github.com/repfly/mimir
cd mimir
pip install -e .          # server
pip install -e client/    # client (optional)
```

---

## Configuration (`mimir.toml`)

Run `mimir init` to generate a template, or create one manually:

```toml
[[repos]]
name = "my-api"
path = "/path/to/my-api"
language_hint = "python"

[[repos]]
name = "my-frontend"
path = "/path/to/my-frontend"
language_hint = "typescript"

[indexing]
summary_mode = "heuristic"   # none | heuristic | llm
excluded_patterns = ["__pycache__", "node_modules", ".git", "venv", ".venv"]
max_file_size_kb = 500
concurrency = 10

[llm]
model = "claude-haiku-4-5-20251001"    # only needed for summary_mode = "llm"
api_key_env = "ANTHROPIC_API_KEY"

[embeddings]
model = "all-mpnet-base-v2"  # local, offline, no API keys needed
batch_size = 64

[vector_db]
backend = "numpy"            # numpy (in-process) | chroma (persistent)

[retrieval]
default_beam_width = 3
default_token_budget = 8000
expansion_hops = 2
hybrid_alpha = 0.7           # balance between semantic and BM25 keyword search
relevance_gate = 0.3

[temporal]
recency_lambda = 0.02
change_window_commits = 100
co_retrieval_enabled = true

[session]
context_decay_turns = 5
topic_tracking_alpha = 0.3
```

### Configuration Reference

| Section | Key | Default | Description |
|---|---|---|---|
| `indexing` | `summary_mode` | `heuristic` | `none` = raw code only; `heuristic` = signatures + docstrings; `llm` = LLM-generated summaries |
| `indexing` | `max_file_size_kb` | `500` | Skip files larger than this |
| `indexing` | `concurrency` | `10` | Parallel file parsing limit |
| `embeddings` | `model` | `jina-embeddings-v2-base-code` | Any sentence-transformers model or Jina API model |
| `vector_db` | `backend` | `numpy` | `numpy` for dev/small projects; `chroma` for persistent production use |
| `retrieval` | `default_token_budget` | `8000` | Maximum tokens per context bundle |
| `retrieval` | `expansion_hops` | `2` | How many graph hops to expand from seed nodes |
| `retrieval` | `relevance_gate` | `0.3` | Minimum score to include expanded nodes |
| `temporal` | `recency_lambda` | `0.02` | Exponential decay rate for recency scoring |
| `session` | `context_decay_turns` | `5` | Turns before previously-sent code is re-included fully |

---

## How It Works

### Indexing Pipeline

```
Source files → TreeSitter parse → Nodes & Edges → CodeGraph (NetworkX)
    → Cross-repo link detection → Summarize (heuristic/llm)
    → Embed all nodes → Persist to SQLite + VectorStore
```

Each node represents a symbol (function, class, method, module) with its code, signature, docstring, and git metadata. Edges encode relationships: `CALLS`, `IMPORTS`, `INHERITS`, `USES_TYPE`, `CONTAINS`, `READS_CONFIG`.

### Retrieval Pipeline

1. **Embed query** — single forward pass through the embedding model
2. **Hierarchical beam search** — find top-K seed nodes by cosine similarity (containers first, then symbols)
3. **Subgraph expansion** — BFS from seeds along typed edges, pruning by relevance gate
4. **Type & config context** — include referenced type definitions and config nodes
5. **Temporal reranking** — score = 0.5x retrieval + 0.2x recency + 0.15x change frequency + 0.15x co-retrieval
6. **Budget fitting** — greedily drop lowest-scoring nodes until token count fits budget
7. **Topological ordering** — order nodes by containment hierarchy for readability

### Session Deduplication

When using `session_id`, Mimir tracks what code has already been sent to the LLM:

| Last sent | Behavior |
|---|---|
| Previous turn | Omitted entirely |
| 2-5 turns ago | Summary only (raw code dropped) |
| 5+ turns ago | Re-included fully |
| Modified since last sent | Always re-included |

Co-retrieval learning tracks which nodes appear together and boosts similar nodes in future queries.

### Incremental Indexing

After the initial full index, `mimir index` runs incrementally:

```bash
mimir index                    # first time: full; subsequent: incremental
git pull                       # pull changes
mimir index                    # only re-indexes the diff
mimir index --clean            # force full re-index (wipes existing data)
```

Mimir stores the last-indexed commit hash per repo. On each run:
1. Computes `git diff` against the stored commit
2. Removes stale nodes (deleted/modified files)
3. Re-parses only changed/added files
4. Embeds only new nodes
5. Persists only the delta

Unchanged repos are skipped entirely.

---

## Serving Modes

Mimir supports three serving modes via the [Model Context Protocol](https://modelcontextprotocol.io/) and HTTP:

| Mode | Command | Package needed | Use case |
|---|---|---|---|
| **Local stdio MCP** | `mimir serve` | `mimir-context-server` | Solo dev with repos on their machine |
| **Shared HTTP server** | `mimir serve --http` | `mimir-context-server` | Central team server that indexes all repos |
| **Remote MCP proxy** | `mimir-client serve <URL>` | `mimir-client` | Dev without local repos queries a shared server |

### Local MCP (Default)

Add to your IDE's MCP config (`~/.cursor/mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir",
      "args": ["serve", "--config", "/path/to/your-project/mimir.toml"]
    }
  }
}
```

### MCP Tools

| Tool | Description |
|---|---|
| `get_context` | Retrieve relevant source code for a natural language query. Call before answering any codebase question. |
| `get_graph_stats` | Node/edge counts, breakdown by kind and repo |
| `get_hotspots` | Recently and frequently modified code |
| `clear_data` | Wipe the index |

Pass a consistent `session_id` on every turn to enable cross-turn deduplication:

```json
{"name": "get_context", "arguments": {"query": "...", "session_id": "conv-abc123"}}
```

---

## Shared HTTP Server (Teams & Enterprise)

For teams where not everyone has access to all repos — mobile devs needing backend context, frontend devs needing API knowledge, or enterprise environments with restricted repo access — Mimir runs as a central HTTP server.

### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Team Server (CI machine, cloud VM, Docker container)         │
│                                                               │
│  Has all repos cloned/mounted:                                │
│    /repos/bff/            (TypeScript)                        │
│    /repos/payment-svc/    (Kotlin)                            │
│    /repos/auth-svc/       (Go)                                │
│    /repos/ios-app/        (Swift)                              │
│                                                               │
│  Runs:                                                        │
│    mimir index               (cron or CI trigger)             │
│    mimir serve --http        (always on, port 8421)           │
└──────────────────────────────┬────────────────────────────────┘
                               │ HTTP (port 8421)
          ┌────────────────────┼───────────────────┐
          │                    │                   │
    ┌─────▼──────┐       ┌─────▼─────┐       ┌─────▼─────┐
    │ Mobile Dev │       │ Backend   │       │ Frontend  │
    │ No repos   │       │ Dev       │       │ No repos  │
    │ cloned     │       │ Has repos │       │ cloned    │
    │            │       │ locally   │       │            │
    │ mimir-     │       │ mimir     │       │ mimir-    │
    │ client     │       │ serve     │       │ client    │
    │ serve      │       │ (local)   │       │ serve     │
    │ http://..  │       │           │       │ http://.. │
    └────────────┘       └───────────┘       └───────────┘
```

### Server Setup (Bare Metal)

```bash
# 1. Create mimir.toml pointing to all team repos
# 2. Index
mimir index --config /repos/mimir.toml

# 3. Start shared HTTP server
mimir serve --http --config /repos/mimir.toml
# → Listening on http://0.0.0.0:8421

# 4. Schedule incremental re-indexing (cron)
# */15 * * * * cd /repos && git -C bff pull -q && mimir index --config /repos/mimir.toml
```

### Client Setup (Devs Without Repos)

No repos to clone. Install the lightweight client and configure your IDE:

```bash
pipx install mimir-server-client
```

Then add to your MCP config:

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir-client",
      "args": ["serve", "http://team-server:8421"]
    }
  }
}
```

This starts a local stdio MCP proxy that forwards all queries to the shared server. Your IDE sees it as a normal MCP server. The client package is tiny (~2 dependencies) — no ML models, no tree-sitter, no indexing.

You can also check server connectivity:

```bash
mimir-client health http://team-server:8421
```

> **Note:** If you have the full `mimir-context-server` installed, `mimir serve --remote http://team-server:8421` also works.

### HTTP API

The shared server exposes a REST API for non-MCP clients (dashboards, CI pipelines, custom tooling):

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/health` | GET | Health check — returns status, workspace name, node/edge counts |
| `/api/v1/context` | POST | Search — `{"query": "...", "budget": 8000, "repos": ["api"], "session_id": "..."}` |
| `/api/v1/stats` | GET | Graph statistics breakdown by kind and repo |
| `/api/v1/hotspots` | GET | Recently/frequently changed code. Optional `?top=20` |
| `/api/v1/clear` | POST | Clear index data — `{"graph": true, "sessions": true}` |
| `/api/v1/mcp` | POST | Raw MCP JSON-RPC passthrough (used by `--remote` proxy) |

---

## Docker Deployment

The Docker image pre-bakes the embedding model (~400MB) into the image layer so the container starts fast and runs fully offline. The entrypoint handles the index-then-serve workflow automatically.

### Build

```bash
docker build -t mimir-server .
```

### Run

```bash
# Auto mode (default): indexes repos, then starts HTTP server
docker run -p 8421:8421 \
  -v /path/to/repos:/project \
  mimir-server

# Serve only (skip indexing, use existing index data)
docker run -p 8421:8421 \
  -v /path/to/repos:/project \
  mimir-server serve

# Serve with auto-indexing on start
docker run -p 8421:8421 \
  -v /path/to/repos:/project \
  -e AUTO_INDEX=1 \
  mimir-server serve

# Run a one-off command (index, search, etc.)
docker run -v /path/to/repos:/project mimir-server index
docker run -v /path/to/repos:/project mimir-server search "auth flow"
```

### Docker Compose

```yaml
services:
  mimir:
    build: .
    ports:
      - "8421:8421"
    volumes:
      # Directory containing mimir.toml and the repos it references
      - /path/to/repos:/project
      # Persistent index data survives container restarts
      - mimir-data:/project/.mimir
    environment:
      - MIMIR_CONFIG=mimir.toml
    restart: unless-stopped

volumes:
  mimir-data:
```

```bash
docker compose up -d
```

### Entrypoint Modes

| CMD | Behavior |
|---|---|
| `auto` (default) | Index all repos from config, then start HTTP server |
| `serve` | Start HTTP server directly (set `AUTO_INDEX=1` to index first) |
| `index` | Run indexing only, then exit |
| `search "query"` | Run a one-off search, then exit |
| Any other `mimir` subcommand | Passed through to the `mimir` CLI |

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MIMIR_CONFIG` | `mimir.toml` | Path to config file (relative to `/project`) |
| `MIMIR_HOST` | `0.0.0.0` | HTTP server bind address |
| `MIMIR_PORT` | `8421` | HTTP server port |
| `AUTO_INDEX` | `0` | Set to `1` to index before serving in `serve` mode |
| `MIMIR_WORKSPACE` | — | Named workspace to use |
| `HF_HUB_OFFLINE` | `1` | Pre-set to offline; embedding model is baked in |

### Health Check

The image includes a Docker `HEALTHCHECK` that polls `/api/v1/health` every 30 seconds with a 60-second startup grace period. Works out of the box with Docker Compose, Kubernetes liveness probes, and AWS ECS.

### Enterprise Deployment Example

For an enterprise team with multiple microservices in separate repos:

```toml
# /repos/mimir.toml
[[repos]]
name = "bff"
path = "./bff"
language_hint = "typescript"

[[repos]]
name = "payment-service"
path = "./payment-service"
language_hint = "kotlin"

[[repos]]
name = "auth-service"
path = "./auth-service"
language_hint = "go"

[[repos]]
name = "ios-app"
path = "./ios-app"
language_hint = "swift"

[indexing]
summary_mode = "heuristic"

[embeddings]
model = "all-mpnet-base-v2"

[cross_repo]
detect_api_contracts = true
detect_shared_imports = true
```

```bash
# Clone all repos into /repos/, place mimir.toml there, then:
docker run -p 8421:8421 -v /repos:/project mimir-server
```

All developers install the lightweight client and connect:

```bash
pipx install mimir-server-client
```

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir-client",
      "args": ["serve", "http://team-server:8421"]
    }
  }
}
```

---

## Multi-Project Workspaces

Manage multiple projects from one installation with full isolation:

```bash
# Register projects
mimir workspace add payment-api  --config /work/payment/mimir.toml
mimir workspace add mobile-app   --config /work/mobile/mimir.toml
mimir workspace list

# Index each
mimir index --workspace payment-api
mimir index --workspace mobile-app

# Search a specific workspace
mimir search "auth flow" --workspace payment-api
```

MCP config — each server is locked to one workspace:

```json
{
  "mcpServers": {
    "mimir-payment":  {"command": "mimir", "args": ["serve", "--workspace", "payment-api"]},
    "mimir-mobile":   {"command": "mimir", "args": ["serve", "--workspace", "mobile-app"]}
  }
}
```

The `MIMIR_WORKSPACE` environment variable is also supported.

---

## Web Inspector

Mimir includes a browser-based graph visualization UI:

```bash
mimir ui                        # launches at http://localhost:8420
mimir ui --port 9000            # custom port
```

Explore nodes by kind and repo, inspect edges, view cross-repo links, and drill into individual symbols.

---

## Data Storage

Mimir stores all index data in a local directory (default `.mimir/`, configurable via `data_dir` in config):

```
.mimir/
├── graph.db          # SQLite: nodes, edges, repo_state, embeddings
├── sessions.db       # SQLite: session state for deduplication
└── chroma/           # ChromaDB data (only if backend = "chroma")
```

---

## Supported Languages

Mimir uses tree-sitter grammars for symbol extraction. Currently supported:

Python, TypeScript, JavaScript, Go, Java, Rust, C, C++, Ruby, Swift, Kotlin, C#, TOML, YAML, JSON

---

## CLI Reference

### `mimir` (server package: `mimir-context-server`)

```
mimir init                  Create a mimir.toml config file
mimir index                 Index all configured repositories
mimir search "query"        Search and assemble context
mimir ask "query"           Interactive search (retrieves context, calls LLM)
mimir serve                 Start the MCP server
mimir ui                    Launch the web inspector (localhost:8420)
mimir hotspots              Show recently/frequently changed code
mimir graph                 Explore the code graph
mimir clear                 Delete locally stored index data
mimir vacuum                Compact the SQLite database
mimir workspace             Manage named workspaces

Index flags:
  --clean                   Force a full re-index (wipes existing data)
  --mode MODE               Summary mode: none, heuristic, llm

Serve modes:
  (default)                 stdio MCP server (local IDE integration)
  --http                    Shared HTTP server (team access)
  --http-port PORT          HTTP port (default: 8421)
  --http-host HOST          HTTP bind address (default: 0.0.0.0)
  --remote / -r URL         Proxy to a remote Mimir HTTP server

Global flags:
  --workspace / -w NAME     Use a named workspace from the registry
  --config    / -c PATH     Path to mimir.toml (default: ./mimir.toml)
  --verbose   / -v          Enable debug logging
```

### `mimir-client` (client package: `mimir-server-client`)

```
mimir-client serve <URL>    Start local MCP proxy to a remote Mimir server
mimir-client health <URL>   Check if a remote Mimir server is reachable

Flags:
  --verbose / -v            Enable debug logging
```

The client package has only 2 dependencies (`aiohttp`, `typer`) and does not require Python 3.11 — it works with Python 3.10+.

---

## Project Structure

```
├── mimir/                      # Server package (mimir-context-server)
│   ├── domain/                 # Core models, config, graph, errors
│   ├── ports/                  # Interface definitions (embedder, parser, stores)
│   ├── services/               # Business logic (indexing, retrieval, temporal, session)
│   ├── infra/                  # Implementations (tree-sitter, embedders, SQLite, ChromaDB)
│   ├── adapters/               # External interfaces (CLI, MCP, HTTP, web UI)
│   └── container.py            # Dependency injection wiring
├── client/                     # Client package (mimir-server-client)
│   └── mimir_client/           # Lightweight MCP proxy + health check CLI
├── tests/                      # Test suite
├── Dockerfile                  # Server container with pre-baked embedding model
├── docker-compose.yml          # Docker Compose for team deployment
├── docker-entrypoint.sh        # Entrypoint: auto index-then-serve
├── pyproject.toml              # Server package metadata
└── mimir.toml                  # Example configuration
```

---

## Contributing

Pull requests welcome. Run the test suite with:

```bash
pip install -e ".[dev]"
pytest
```

### Publishing to PyPI

```bash
pip install build twine

# Server package
python -m build
twine upload dist/*

# Client package
cd client
python -m build
twine upload dist/*
```

---

## License

MIT
