"""MCP adapter sub-package — stdio and remote MCP transports."""

from mimir.adapters.mcp.server import run_mcp_server
from mimir.adapters.mcp.remote import run_remote_mcp

__all__ = ["run_mcp_server", "run_remote_mcp"]
