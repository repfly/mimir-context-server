# @mimir/plugin-catalog-backend-module-mimir

Backstage catalog backend module that auto-populates your service catalog from Mimir's code graph analysis. Instead of manually maintaining `catalog-info.yaml` files, this plugin discovers services, APIs, dependencies, tech stacks, and ownership directly from your code.

## Prerequisites

- A running Mimir HTTP server with indexed repositories (`mimir serve --http`)
- Backstage instance (v1.0+)

## Installation

```bash
yarn --cwd packages/backend add @mimir/plugin-catalog-backend-module-mimir
```

Register the module in `packages/backend/src/index.ts`:

```typescript
backend.add(import('@mimir/plugin-catalog-backend-module-mimir'));
```

## Configuration

Add to your `app-config.yaml`:

```yaml
catalog:
  providers:
    mimir:
      baseUrl: http://localhost:8421  # Mimir HTTP server URL
      refreshIntervalMinutes: 30      # How often to sync (default: 30)
      repoFilters:                    # Optional: limit to specific repos
        - payments-service
        - shared-lib
```

## What Gets Created

### Component Entities

One Component per indexed repository:

| Field | Source |
|---|---|
| `metadata.name` | Repository name (kebab-cased) |
| `spec.type` | `service` if APIs detected, `library` otherwise |
| `spec.owner` | Top git committer as `user:<email>`, or `unknown` |
| `spec.providesApis` | API entities this service exposes |
| `spec.consumesApis` | Services called via `api_calls` edges |
| `spec.dependsOn` | Libraries linked via `shared_lib`/`imports` edges |
| `metadata.tags` | Detected languages + frameworks |

### API Entities

One API entity per discovered endpoint (route decorators like `@app.get("/path")`):

| Field | Source |
|---|---|
| `metadata.name` | `{repo}-{method}-{path}` (kebab-cased) |
| `spec.type` | `openapi` |
| `spec.owner` | Inherited from parent service |

### Annotations

All entities include Mimir-specific annotations:

| Annotation | Description | On |
|---|---|---|
| `mimir.dev/repo` | Repository name | Component, API |
| `mimir.dev/quality-score` | Code quality score (0-1) | Component |
| `mimir.dev/node-id` | Mimir graph node ID | Component, API |
| `mimir.dev/method` | HTTP method | API |
| `mimir.dev/path` | API route path | API |

## Ownership

The plugin infers ownership from git history — the most frequent committer to a repository becomes the owner. The format follows Backstage conventions: `user:email@example.com`.

To override ownership for specific services, add a `catalog-info.yaml` alongside the Mimir-generated entities. Backstage merges metadata from multiple providers.

## API Endpoints (Mimir Server)

The plugin consumes these Mimir HTTP endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/catalog` | Full catalog generation |
| `GET /api/v1/catalog/{repo}` | Single service entry |
| `POST /api/v1/catalog/drift` | Dependency drift detection |

## Drift Detection

Compare what your `catalog-info.yaml` declares vs. what the code actually shows:

```bash
curl -X POST http://localhost:8421/api/v1/catalog/drift \
  -H 'Content-Type: application/json' \
  -d '{
    "repo": "payments-service",
    "declared_dependencies": [
      {"name": "users-service", "type": "api"},
      {"name": "shared-lib", "type": "library"}
    ]
  }'
```

Response includes `confirmed`, `undeclared` (in code but not declared), and `missing_in_code` (declared but not found) entries with a `drift_score` from 0 (perfect) to 1 (fully mismatched).

## Troubleshooting

**No entities appearing:**
- Verify Mimir server is running: `curl http://localhost:8421/api/v1/health`
- Check that repos are indexed: `curl http://localhost:8421/api/v1/stats`
- Check Backstage backend logs for `mimir-provider` messages

**Owner shows as "unknown":**
- The repository must be a git repo with commit history
- Mimir server needs filesystem access to the repo path configured in `mimir.toml`

**Stale data:**
- Decrease `refreshIntervalMinutes` in config
- Re-index after code changes: `mimir index --incremental`
