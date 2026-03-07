# Mimir — Context Server

> *In Norse mythology, Mímir was the wisest being in all the Nine Realms — guardian of the Well of Wisdom beneath Yggdrasil, the World Tree. Odin sacrificed his eye for a single drink from that well. **Mimir** brings that same depth of knowledge to your codebase.*

[![PyPI](https://img.shields.io/pypi/v/mimir)](https://pypi.org/project/mimir/)
[![Python](https://img.shields.io/pypi/pyversions/mimir)](https://pypi.org/project/mimir/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Mimir** is an intelligent context engine that helps LLMs understand large, multi-repo codebases. Instead of dumping raw files into a prompt, Mimir builds a semantic code graph, ranks nodes by relevance and recency, and assembles a minimal, connected, token-budget-aware context bundle — exactly what the model needs, nothing it doesn't.

---

## The Problem

When you ask Claude or GPT to help with a large codebase, you face a brutal choice:

- **Too little context** → the model hallucinates or misses related code
- **Too much context** → you burn tokens on irrelevant files and hit limits
- **Copy-paste** → fragile, manual, doesn't scale across repos

## The Solution

Mimir indexes your code into a hierarchical graph of repositories → files → classes → functions, embeds every node semantically, and at query time performs a beam search across the graph to find the tightest connected subgraph that answers your question — within your token budget.

---

## Key Features

- 🌲 **Hierarchical beam search** — finds connected code paths, not isolated snippets
- 🔗 **Subgraph expansion** — automatically surfaces callers, callees, and sibling nodes
- ⏱️ **Temporal reranking** — recently changed code scores higher
- 💬 **Session deduplication** — code already sent to the LLM is summarised or omitted on subsequent turns
- 🏢 **Multi-repo** — single server spans multiple repositories, with cross-repo edge detection
- 🔒 **Workspace isolation** — per-project indexes, agents can't cross project boundaries
- 📡 **MCP server** — plug-and-play with Claude Desktop, Cursor, and any MCP-compatible IDE
- 🐳 **Docker-first** — zero Python setup, embedding model pre-baked in the image
- 🔌 **100% offline** — local embedding model, no API keys required for indexing

---

## Quick Start

```bash
pip install mimir
cd /your/project
mimir init          # creates mimir.toml
mimir index         # builds the semantic code graph
mimir search "how does authentication work?"
mimir serve         # start MCP server for your IDE
```

---

## Installation

### pip (recommended)
```bash
pip install mimir
```

### Docker (zero Python setup)
```bash
docker pull repfly/mimir:latest
```

### From source
```bash
git clone https://github.com/repfly/mimir
cd mimir
pip install -e .
```

---

## Configuration (`mimir.toml`)

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

[embeddings]
model = "all-mpnet-base-v2"  # local, offline, no API keys needed
batch_size = 64

[vector_db]
backend = "numpy"            # numpy (in-process) | chroma (persistent)

[retrieval]
default_beam_width = 3
default_token_budget = 8000
expansion_hops = 2
relevance_gate = 0.3

[temporal]
recency_lambda = 0.02
co_retrieval_enabled = true

[session]
context_decay_turns = 5
```

### Key Options

| Key | Default | Description |
|---|---|---|
| `indexing.summary_mode` | `heuristic` | `none` = raw code only; `heuristic` = signatures + docstrings; `llm` = LLM-generated summaries |
| `embeddings.model` | `all-mpnet-base-v2` | Any `sentence-transformers` model name |
| `vector_db.backend` | `numpy` | `numpy` = in-process; `chroma` = persistent ChromaDB |
| `retrieval.default_token_budget` | `8000` | Maximum tokens per context bundle |
| `retrieval.expansion_hops` | `2` | How many hops to expand from seed nodes in the graph |
| `temporal.recency_lambda` | `0.02` | Decay rate for recency scoring |

---

## MCP Server Setup

Mimir speaks the [Model Context Protocol](https://modelcontextprotocol.io/) and supports three serving modes:

| Mode | Command | Who uses it |
|---|---|---|
| **Local stdio** | `mimir serve` | Solo dev with repos on their machine |
| **Shared HTTP** | `mimir serve --http` | Team server that indexes all repos |
| **Remote proxy** | `mimir serve --remote <URL>` | Dev who queries a shared server |

### Local Mode (Default)

Add to your MCP config (`~/.cursor/mcp.json` or `claude_desktop_config.json`):

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

### Shared Server Mode (Teams)

One machine indexes all repos and serves context over HTTP. See [Shared Server](#shared-server-for-teams) for the full setup guide.

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir",
      "args": ["serve", "--remote", "http://team-server:8421"]
    }
  }
}
```

### Docker (no Python needed on the host)

```json
{
  "mcpServers": {
    "mimir": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/path/to/your-project:/project",
        "yourusername/mimir:latest",
        "serve", "--config", "/project/mimir.toml"
      ]
    }
  }
}
```

### Available MCP Tools

| Tool | When to use |
|---|---|
| `get_context` | Before answering any question about the codebase |
| `get_graph_stats` | To confirm indexing succeeded or check what's indexed |
| `get_hotspots` | To find recently active or frequently changed code |
| `clear_data` | To wipe and reset the index |

### Session deduplication

Pass a consistent `session_id` on every turn to avoid re-sending code the model has already seen:

```json
{"name": "get_context", "arguments": {"query": "...", "session_id": "conv-abc123"}}
```

---

## Incremental Indexing

After the initial full index, `mimir index` will automatically run incrementally. It re-indexes only files that changed since the last indexed git commit:

```bash
mimir index                    # first time: full index, subsequent: incremental
git pull                       # pull changes to your repos
mimir index                    # only re-index the diff (seconds, not minutes)
mimir index --clean            # explicitly force a full re-index (wipes existing data)
```

Mimir stores the last-indexed commit hash per repo. On each run it:

1. Computes `git diff` against the stored commit
2. Removes stale nodes (deleted/modified files)
3. Re-parses only changed/added files
4. Embeds only the new nodes
5. Persists only the delta to storage

Per-repo granularity means unchanged repos are skipped entirely. If a repo has never been indexed, it falls back to full indexing for that repo only.

```
✓ Incremental index complete
  bff:           updated (a1b2c3d4 → e5f6g7h8)
    Files: +2 added, ~3 modified, -1 deleted
    Parsed: 5 files, 18 symbols
  payment-svc:   up to date (i9j0k1l2)
  ios-app:       up to date (m3n4o5p6)
```

---

## Shared Server for Teams

For teams where not everyone wants to clone all repos locally (e.g. a mobile developer who needs context from backend microservices), Mimir supports a **shared HTTP server** mode.

### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Team Server (CI machine, cloud VM, or any shared host)      │
│                                                              │
│  Has all repos cloned:                                       │
│    /repos/bff/          (TypeScript)                         │
│    /repos/payment-svc/  (Kotlin)                             │
│    /repos/auth-svc/     (Go)                                 │
│    /repos/ios-app/      (Swift)                              │
│                                                              │
│  Runs:                                                       │
│    mimir index                 (cron or CI trigger)          │
│    mimir serve --http          (always on, port 8421)        │
└──────────────────────────────┬───────────────────────────────┘
                               │ HTTP (port 8421)
          ┌────────────────────┼───────────────────┐
          │                    │                   │
    ┌─────▼──────┐       ┌─────▼─────┐       ┌─────▼─────┐
    │ Mobile Dev │       │ Backend   │       │ Frontend  │
    │            │       │ Dev       │       │ Dev       │
    │ No repos   │       │ Has repos │       │ No repos  │
    │ cloned     │       │ locally   │       │ cloned    │
    │            │       │           │       │           │
    │ mimir      │       │ mimir     │       │ mimir     │
    │ serve      │       │ serve     │       │ serve     │
    │ --remote   │       │ (default) │       │ --remote  │
    │ http://..  │       │           │       │ http://.. │
    └────────────┘       └───────────┘       └───────────┘
```

### Server Setup

```bash
# 1. Create mimir.toml with all team repos
[[repos]]
name = "bff"
path = "/repos/bff"
language_hint = "typescript"

[[repos]]
name = "payment-service"
path = "/repos/payment-service"
language_hint = "kotlin"

[[repos]]
name = "auth-service"
path = "/repos/auth-service"
language_hint = "go"

[[repos]]
name = "ios-app"
path = "/repos/ios-app"
language_hint = "swift"

[indexing]
summary_mode = "heuristic"

[embeddings]
model = "all-mpnet-base-v2"
EOF

# 2. Index all repos
mimir index --config /repos/mimir.toml

# 3. Start the shared HTTP server
mimir serve --http --config /repos/mimir.toml
# → Listening on http://0.0.0.0:8421
```

To keep the index fresh, schedule incremental indexing with cron or a CI webhook:

```bash
# crontab — re-index every 15 minutes
*/15 * * * * cd /repos && git -C bff pull -q && git -C payment-service pull -q && mimir index --config /repos/mimir.toml
```

### Client Setup (Mobile/Frontend Devs)

No repos to clone. Just configure your IDE:

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir",
      "args": ["serve", "--remote", "http://team-server:8421"]
    }
  }
}
```

This starts a local stdio MCP proxy that forwards all queries to the shared server. Your IDE sees it as a normal MCP server.

### REST API

The shared server also exposes a REST API for non-MCP clients:

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/health` | GET | Health check + graph stats |
| `/api/v1/context` | POST | Search — `{"query": "...", "budget": 8000}` |
| `/api/v1/stats` | GET | Graph statistics |
| `/api/v1/hotspots` | GET | Recently changed code |
| `/api/v1/mcp` | POST | Raw MCP JSON-RPC passthrough |

---

## Multi-Project Workspaces

Mimir supports named workspaces so you can manage multiple projects from one installation, with full isolation between them:

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

MCP config — each server is locked to one workspace at startup:

```json
{
  "mcpServers": {
    "mimir-payment":  {"command": "mimir", "args": ["serve", "--workspace", "payment-api"]},
    "mimir-mobile":   {"command": "mimir", "args": ["serve", "--workspace", "mobile-app"]}
  }
}
```

The `MIMIR_WORKSPACE` environment variable is also supported as a fallback.

---

## CLI Reference

```
mimir init          Create a mimir.toml config file
mimir index         Index all configured repositories
mimir search        Search and assemble context for a query
mimir ask           Interactive semantic search (retrieves context and calls LLM)
mimir serve         Start the MCP server
mimir ui            Launch the web inspector at localhost:8420
mimir hotspots      Show recently and frequently changed code
mimir graph         Explore the code graph
mimir clear         Delete locally stored index data
mimir vacuum        Compact the SQLite graph database to reclaim unused file space
mimir workspace     Manage named workspaces

index flags:
  --clean                Force a full re-index (wipes existing data)
  --mode MODE            Summary mode: none, heuristic, llm

serve modes:
  (default)              stdio MCP server (for local IDE integration)
  --http                 Shared HTTP server (for team access)
  --http-port PORT       Port for HTTP server (default: 8421)
  --http-host HOST       Bind address for HTTP server (default: 0.0.0.0)
  --remote / -r URL      Proxy to a remote Mimir HTTP server

Global flags (all commands):
  --workspace / -w NAME    Use a named workspace from the registry
  --config    / -c PATH    Path to mimir.toml (default: ./mimir.toml)
  --verbose   / -v         Enable debug logging
```
---

## Contributing

Pull requests welcome. Run the test suite with:

```bash
pip install -e ".[dev]"
pytest
```

---

## License

MIT © 2026

