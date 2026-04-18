"""Protocol helpers and tool metadata for stdio MCP."""

from __future__ import annotations

from typing import Any


def response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def initialize_result(workspace_name: str) -> dict:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": "mimir",
            "version": "1.0.0",
            "workspace": workspace_name,
        },
    }


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "get_context",
            "description": "Retrieve relevant code context for a natural language query.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language question or task."},
                    "budget": {"type": "integer", "description": "Maximum token count. Default: 8000."},
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo filter.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Conversation session ID for deduplication.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_graph_stats",
            "description": "Return graph statistics and indexed repositories.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_hotspots",
            "description": "Return the most active or recently changed code nodes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "description": "Number of hotspots to return. Default: 20."},
                },
            },
        },
        {
            "name": "get_write_context",
            "description": "Collect edit-time context for a target file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path or suffix to inspect."},
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "get_impact",
            "description": "Analyze the blast radius of a file or symbol change.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Target node ID when available."},
                    "file_path": {"type": "string", "description": "File path to narrow the target."},
                    "symbol_name": {"type": "string", "description": "Symbol name to analyze."},
                    "max_hops": {"type": "integer", "description": "Maximum transitive depth. Default: 3."},
                },
            },
        },
        {
            "name": "get_quality",
            "description": "Analyze graph connectivity quality and gap nodes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo filter.",
                    },
                    "threshold": {"type": "number", "description": "Gap threshold. Default: 0.3."},
                    "top_n": {"type": "integer", "description": "Max gap nodes to return. Default: 50."},
                },
            },
        },
        {
            "name": "get_catalog",
            "description": "Generate a catalog view from the code graph.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional repo filter.",
                    },
                },
            },
        },
        {
            "name": "get_catalog_drift",
            "description": "Compare declared dependencies against graph-derived dependencies.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name."},
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
                        "description": "Declared dependencies to compare.",
                    },
                },
                "required": ["repo", "declared_dependencies"],
            },
        },
        {
            "name": "validate_change",
            "description": "Validate a diff against architectural guardrails.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "diff": {"type": "string", "description": "Unified diff text."},
                    "rules_path": {"type": "string", "description": "Path to guardrail rules."},
                },
                "required": ["diff"],
            },
        },
        {
            "name": "can_i_modify",
            "description": "Check whether a file is within the current agent policy.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to check."},
                    "policy_path": {"type": "string", "description": "Path to mimir-agent-policy.yaml."},
                },
                "required": ["file_path"],
            },
        },
    ]
