# CLI Reference

> [Back to README](../README.md)

## `mimir` (server package: `mimir-context-server`)

```
mimir init                  Create a mimir.toml config file
mimir index                 Index all configured repositories
mimir search "query"        Search and assemble context
mimir ask "query"           Interactive search (retrieves context, calls LLM)
mimir serve                 Start the MCP server
mimir ui                    Launch the web inspector (localhost:8420)
mimir hotspots              Show recently/frequently changed code
mimir quality               Analyze graph quality and detect gaps
mimir graph                 Explore the code graph
mimir clear                 Delete locally stored index data
mimir vacuum                Compact the SQLite database
mimir guardrail check       Validate a diff against architectural rules
mimir guardrail init        Generate example rules + agent policy files
mimir guardrail test        Dry-run: validate rule syntax against current graph
mimir guardrail approve <rule-ids...> --reason "..."
                            Create an empty commit carrying a Mimir-Approved
                            trailer on HEAD to clear matching BLOCK violations
mimir workspace             Manage named workspaces

Index flags:
  --clean                   Force a full re-index (wipes existing data)
  --mode MODE               Summary mode: none, heuristic

Serve modes:
  (default)                 stdio MCP server (local IDE integration)
  --http                    Shared HTTP server (team access)
  --http-port PORT          HTTP port (default: 8421)
  --http-host HOST          HTTP bind address (default: 0.0.0.0)
  --remote / -r URL         Proxy to a remote Mimir HTTP server
  --watch                   Enable live file watching (re-indexes on save)

Global flags:
  --workspace / -w NAME     Use a named workspace from the registry
  --config    / -c PATH     Path to mimir.toml (default: ./mimir.toml)
  --verbose   / -v          Enable debug logging
```

## `mimir-client` (client package: `mimir-server-client`)

```
mimir-client serve <URL>    Start local MCP proxy to a remote Mimir server
mimir-client health <URL>   Check if a remote Mimir server is reachable

Flags:
  --verbose / -v            Enable debug logging
```

The client package has only 2 dependencies (`aiohttp`, `typer`) and does not require Python 3.11 — it works with Python 3.10+.
