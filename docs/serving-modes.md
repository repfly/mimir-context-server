# Serving Modes

> [Back to README](../README.md)

Mimir supports three serving modes via the [Model Context Protocol](https://modelcontextprotocol.io/) and HTTP:

| Mode | Command | Package needed | Use case |
|---|---|---|---|
| **Local stdio MCP** | `mimir query serve` | `mimir-context-server` | Solo dev querying a published local index |
| **Shared HTTP server** | `mimir query serve --http` | `mimir-context-server` | Query-only server over the active published index |
| **Remote MCP proxy** | `mimir-client serve <URL>` | `mimir-client` | Dev without local repos queries a shared server |

## Local MCP (Default)

Add to your IDE's MCP config (`~/.cursor/mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir",
      "args": ["query", "serve", "--config", "/path/to/your-project/mimir.toml"]
    }
  }
}
```

Build the local index separately:

```bash
mimir indexer run --config /path/to/your-project/mimir.toml
```

## MCP Tools

| Tool | Description |
|---|---|
| `get_context` | Retrieve relevant source code for a natural language query. Call before answering any codebase question. |
| `get_write_context` | Get edit-time context for a target file from the active index. |
| `get_impact` | Analyze what would break if you change a symbol or file. |
| `get_quality` | Analyze graph connectivity quality and detect gaps — nodes with missing or weak connections that may indicate under-indexed areas. |
| `get_catalog` | Generate a Backstage-compatible service catalog: services, APIs, dependencies, tech stack, ownership, and quality scores. |
| `get_catalog_drift` | Compare declared service dependencies against code-analyzed reality. Detects undeclared and missing dependencies with a drift score. |
| `validate_change` | Validate a diff against architectural rules using the active index. |
| `can_i_modify` | Check if a file is within the agent's allowed scope per the agent policy. |
| `get_graph_stats` | Node/edge counts, breakdown by kind and repo |
| `get_hotspots` | Recently and frequently modified code |

Pass a consistent `session_id` on every turn to enable cross-turn deduplication:

```json
{"name": "get_context", "arguments": {"query": "...", "session_id": "conv-abc123"}}
```

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
│    mimir indexer worker      (queue-driven indexer)           │
│    mimir query serve --http  (always on, port 8421)           │
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

### Server Setup

```bash
mimir indexer run --config /repos/mimir.toml
mimir query serve --http --config /repos/mimir.toml
# → Listening on http://0.0.0.0:8421
```

### Client Setup

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

> If you have the full `mimir-context-server` installed, `mimir query serve --remote http://team-server:8421` also works.

## HTTP API

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/health` | GET | Health check — returns status, workspace name, node/edge counts |
| `/api/v1/index/status` | GET | Active published index version, repo commits, and graph counts |
| `/api/v1/context` | POST | Search — `{"query": "...", "budget": 8000, "repos": ["api"], "session_id": "..."}` |
| `/api/v1/write_context` | POST | Write-path context — `{"file_path": "src/auth/login.py"}` |
| `/api/v1/impact` | POST | Impact analysis — `{"symbol_name": "AuthService", "max_hops": 3}` |
| `/api/v1/stats` | GET | Graph statistics breakdown by kind and repo |
| `/api/v1/hotspots` | GET | Recently/frequently changed code. Optional `?top=20` |
| `/api/v1/quality` | GET | Graph quality overview and gap detection. Optional `?threshold=0.3&top_n=50&repos=my-api` |
| `/api/v1/catalog` | GET | Backstage-compatible service catalog. Optional `?repos=svc-a,svc-b` |
| `/api/v1/catalog/{repo}` | GET | Single-service catalog entry |
| `/api/v1/catalog/drift` | POST | Dependency drift detection — `{"repo": "my-api", "declared_dependencies": [{"name": "svc-b"}]}` |
| `/api/v1/guardrails/check` | POST | Architectural guardrail check — `{"diff": "...", "rules_path": "mimir-rules.yaml"}` |
| `/api/v1/mcp` | POST | Raw MCP JSON-RPC passthrough (used by `--remote` proxy) |

See also: [Docker](docker.md) for containerized deployment, [Configuration](configuration.md) for `mimir.toml`.
