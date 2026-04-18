"""Shared MCP helpers for the HTTP adapter."""

from __future__ import annotations

from typing import Any


def rpc_ok(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def tool_definitions() -> list[dict]:
    """Return the MCP tools list (same as stdio MCP server)."""
    return [
        {
            "name": "get_context",
            "description": (
                "Retrieve relevant source code context for a natural language query. "
                "Call this BEFORE answering any question about how the codebase works, "
                "what a function does, where a feature is implemented, or how components interact. "
                "Returns a minimal, connected, token-budget-aware context bundle assembled from "
                "the code graph. Use `session_id` to enable cross-turn deduplication. "
                "Use `repos` to restrict results to specific repositories. "
                "Use `budget` to control the maximum token count (default 8000)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language question or task description"},
                    "budget": {"type": "integer", "description": "Maximum tokens in the context bundle. Default: 8000."},
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo names to restrict search to.",
                    },
                    "session_id": {"type": "string", "description": "Conversation session ID for deduplication."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_graph_stats",
            "description": "Return statistics about the indexed code graph: node count, edge count, breakdown by kind, and repos indexed.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_hotspots",
            "description": "Return the most recently and frequently modified code nodes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "description": "Number of hotspots to return. Default: 20."},
                },
            },
        },
        {
            "name": "get_quality",
            "description": "Analyze graph connectivity quality and detect gaps — nodes with missing connections.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo names to restrict analysis to.",
                    },
                    "threshold": {"type": "number", "description": "Quality threshold for gap detection. Default: 0.3."},
                    "top_n": {"type": "integer", "description": "Max gap nodes to return. Default: 50."},
                },
            },
        },
        {
            "name": "get_catalog",
            "description": "Generate a Backstage-compatible service catalog from the code graph. Returns services with APIs, dependencies, tech stack, ownership, and quality.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo names to include. Omit for all repos.",
                    },
                },
            },
        },
        {
            "name": "get_catalog_drift",
            "description": "Compare declared dependencies against code-analyzed reality. Returns drift score and categorized findings.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name to check."},
                    "declared_dependencies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                            },
                            "required": ["name"],
                        },
                        "description": "Declared dependencies to compare against.",
                    },
                },
                "required": ["repo", "declared_dependencies"],
            },
        },
    ]
