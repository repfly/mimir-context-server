# Backstage Integration

> [Back to README](../README.md)

Mimir includes a Backstage catalog backend module that auto-populates your service catalog directly from the code graph — no manual `catalog-info.yaml` maintenance.

## What Gets Auto-Discovered

| Data | Source |
|---|---|
| **Services** | Each indexed repository becomes a Component entity |
| **APIs** | Route decorators (`@app.get`, `@router.post`, etc.) become API entities |
| **Dependencies** | Cross-repo `CALLS`, `IMPORTS`, `SHARED_LIB` edges become `dependsOn`/`consumesApis` relations |
| **Tech stack** | File extensions → languages, import analysis → frameworks (Flask, FastAPI, Spring, etc.) |
| **Ownership** | Top git committer per repo → Backstage `owner` field (`user:email@example.com`) |
| **Quality scores** | Graph connectivity metrics per service |

## Setup

1. Start the Mimir HTTP server: `mimir serve --http`
2. Install the plugin in your Backstage backend:
   ```bash
   yarn --cwd packages/backend add @mimir/plugin-catalog-backend-module-mimir
   ```
3. Register it in `packages/backend/src/index.ts`:
   ```typescript
   backend.add(import('@mimir/plugin-catalog-backend-module-mimir'));
   ```
4. Configure in `app-config.yaml`:
   ```yaml
   catalog:
     providers:
       mimir:
         baseUrl: http://localhost:8421
         refreshIntervalMinutes: 30
   ```

## Dependency Drift Detection

Compare what your catalog declares vs. what the code actually shows:

```bash
curl -X POST http://localhost:8421/api/v1/catalog/drift \
  -H 'Content-Type: application/json' \
  -d '{"repo": "my-api", "declared_dependencies": [{"name": "auth-service"}]}'
```

Returns `confirmed`, `undeclared` (in code but not declared), and `missing_in_code` entries with a drift score from 0.0 (perfect match) to 1.0 (fully mismatched).

See the [plugin README](../backstage-plugin/plugins/catalog-backend-module-mimir/README.md) for full documentation.

See also: [Serving Modes](serving-modes.md) for HTTP API endpoints.
