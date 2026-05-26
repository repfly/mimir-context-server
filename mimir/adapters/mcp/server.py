"""MCP server adapter — stdio transport bootstrap."""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from mimir.adapters.mcp.stdio_dispatch import handle_request
from mimir.domain.config import MimirConfig
from mimir.runtime import RuntimeFactory

logger = logging.getLogger(__name__)


def run_mcp_server(config: MimirConfig, workspace_name: str | None = None) -> None:
    """Start the MCP stdio server."""
    ws_label = workspace_name or "default"
    runtime = RuntimeFactory(config).query()
    graph = runtime.graph
    logger.info("MCP server starting — workspace=%s, graph has %d nodes", ws_label, graph.node_count)

    try:
        asyncio.run(_main_loop(runtime, ws_label))
    finally:
        runtime.close()


async def _main_loop(
    runtime,
    workspace_name: str,
) -> None:
    """Read JSON-RPC messages from stdin and write responses to stdout."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_event_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    write_transport, _ = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)

    buffer = b""
    while True:
        try:
            data = await reader.read(4096)
            if not data:
                break
            buffer += data
            buffer = await _drain_messages(
                buffer,
                write_transport,
                lambda request: handle_request(runtime, workspace_name, request),
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("MCP loop error: %s", exc, exc_info=True)


async def _drain_messages(buffer: bytes, write_transport, request_handler) -> bytes:
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = await request_handler(request)
        if response and response.get("id") is not None:
            write_transport.write((json.dumps(response) + "\n").encode())
    return buffer
