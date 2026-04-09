# Docker Deployment

> [Back to README](../README.md)

The Docker image pre-bakes the embedding model (~400MB) into the image layer so the container starts fast and runs fully offline. The entrypoint handles the index-then-serve workflow automatically.

## Build

```bash
docker build -t mimir-server .
```

## Run

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

## Docker Compose

```yaml
services:
  mimir:
    build: .
    ports:
      - "8421:8421"
    volumes:
      - /path/to/repos:/project
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

## Entrypoint Modes

| CMD | Behavior |
|---|---|
| `auto` (default) | Index all repos from config, then start HTTP server |
| `serve` | Start HTTP server directly (set `AUTO_INDEX=1` to index first) |
| `index` | Run indexing only, then exit |
| `search "query"` | Run a one-off search, then exit |
| Any other `mimir` subcommand | Passed through to the `mimir` CLI |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MIMIR_CONFIG` | `mimir.toml` | Path to config file (relative to `/project`) |
| `MIMIR_HOST` | `0.0.0.0` | HTTP server bind address |
| `MIMIR_PORT` | `8421` | HTTP server port |
| `AUTO_INDEX` | `0` | Set to `1` to index before serving in `serve` mode |
| `MIMIR_WORKSPACE` | — | Named workspace to use |
| `HF_HUB_OFFLINE` | `1` | Pre-set to offline; embedding model is baked in |

## Health Check

The image includes a Docker `HEALTHCHECK` that polls `/api/v1/health` every 30 seconds with a 60-second startup grace period. Works out of the box with Docker Compose, Kubernetes liveness probes, and AWS ECS.

## Enterprise Deployment Example

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
docker run -p 8421:8421 -v /repos:/project mimir-server
```

All developers install the lightweight client:

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

See also: [Serving Modes](serving-modes.md) for HTTP API details, [Configuration](configuration.md) for `mimir.toml`.
