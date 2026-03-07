# mimir-server-client

[![PyPI](https://img.shields.io/pypi/v/mimir-server-client)](https://pypi.org/project/mimir-server-client/)
[![Python](https://img.shields.io/pypi/pyversions/mimir-server-client)](https://pypi.org/project/mimir-server-client/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/repfly/mimir-context-server/blob/main/LICENSE)

Lightweight MCP proxy client for remote [Mimir](https://github.com/repfly/mimir-context-server) context servers.

No repos, no models, no indexing needed. Just point at a running Mimir HTTP server and get instant MCP access in your IDE.

## Install

```bash
pipx install mimir-server-client
```

## Usage

### MCP proxy (for IDE integration)

```bash
mimir-client serve http://your-server:8421
```

Configure your IDE's MCP settings:

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir-client",
      "args": ["serve", "http://your-server:8421"]
    }
  }
}
```

### Health check

```bash
mimir-client health http://your-server:8421
```

## How it works

`mimir-client` runs a local MCP stdio server that proxies all requests to a remote Mimir HTTP server. This lets developers use Mimir without needing the full server, repo access, or GPU for embeddings.

## License

MIT
