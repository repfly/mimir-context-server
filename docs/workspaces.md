# Multi-Project Workspaces

> [Back to README](../README.md)

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

See also: [Serving Modes](serving-modes.md), [CLI Reference](cli-reference.md).
