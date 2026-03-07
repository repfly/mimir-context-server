"""Thin MCP stdio proxy that forwards to a remote Mimir HTTP server.

This is what mobile/frontend developers use in their IDE config.
It reads JSON-RPC from stdin, POSTs to the remote HTTP ``/api/v1/mcp``
endpoint, and writes the response back to stdout.

No local repos, no local index, no local embedding model required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)


def run_remote_mcp(remote_url: str) -> None:
    """Start a local MCP stdio server that proxies to a remote Mimir HTTP server.

    Parameters
    ----------
    remote_url:
        Base URL of the remote Mimir HTTP server, e.g. ``http://team-server:8421``.
    """
    # Normalise URL
    remote_url = remote_url.rstrip("/")
    mcp_endpoint = f"{remote_url}/api/v1/mcp"
    health_endpoint = f"{remote_url}/api/v1/health"

    logger.info("Remote MCP proxy starting — forwarding to %s", mcp_endpoint)

    async def _check_health() -> bool:
        """Verify the remote server is reachable before entering the main loop."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(health_endpoint, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.info(
                            "Remote server OK — workspace=%s, nodes=%s",
                            data.get("workspace", "?"),
                            data.get("graph_nodes", "?"),
                        )
                        return True
                    else:
                        logger.error("Remote server returned HTTP %d", resp.status)
                        return False
        except Exception as exc:
            logger.error("Cannot reach remote server at %s: %s", remote_url, exc)
            return False

    async def _forward_request(rpc_request: dict) -> dict:
        """POST a JSON-RPC request to the remote server and return the response."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    mcp_endpoint,
                    json=rpc_request,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        body = await resp.text()
                        logger.error("Remote error HTTP %d: %s", resp.status, body[:200])
                        return {
                            "jsonrpc": "2.0",
                            "id": rpc_request.get("id"),
                            "error": {
                                "code": -32000,
                                "message": f"Remote server error: HTTP {resp.status}",
                            },
                        }
        except asyncio.TimeoutError:
            return {
                "jsonrpc": "2.0",
                "id": rpc_request.get("id"),
                "error": {"code": -32000, "message": "Remote server timeout"},
            }
        except Exception as exc:
            logger.error("Forward failed: %s", exc)
            return {
                "jsonrpc": "2.0",
                "id": rpc_request.get("id"),
                "error": {"code": -32000, "message": f"Connection failed: {exc}"},
            }

    async def main_loop():
        """Read JSON-RPC from stdin, forward to remote, write response to stdout."""
        # Health check first
        if not await _check_health():
            logger.error("Exiting — remote server is not reachable")
            sys.exit(1)

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        loop = asyncio.get_event_loop()
        write_transport, _ = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout,
        )

        buffer = b""
        while True:
            try:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                        response = await _forward_request(request)
                        if response and response.get("id") is not None:
                            out = json.dumps(response) + "\n"
                            write_transport.write(out.encode())
                    except json.JSONDecodeError:
                        continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Proxy loop error: %s", exc, exc_info=True)

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
