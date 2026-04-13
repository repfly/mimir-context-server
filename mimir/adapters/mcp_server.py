"""MCP server adapter — stdio transport for IDE integration."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from mimir.container import Container
from mimir.domain.config import MimirConfig

logger = logging.getLogger(__name__)


def run_mcp_server(config: MimirConfig, workspace_name: str | None = None) -> None:
    """Start the MCP server on stdio.

    Parameters
    ----------
    config:
        Validated Mimir configuration.
    workspace_name:
        The workspace name this server is locked to (for informational purposes only).
        If None, the server is running against a bare --config file.
    """
    _ws_label = workspace_name or "default"
    container = Container(config)
    graph = container.load_graph()
    container.warmup()
    logger.info(
        "MCP server starting — workspace=%s, graph has %d nodes",
        _ws_label, graph.node_count,
    )

    # File watcher (started later, after the event loop is available)
    _watcher_enabled = config.watcher.enabled

    async def handle_request(request: dict) -> dict:
        """Route MCP JSON-RPC requests."""
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        try:
            if method == "initialize":
                return _response(request_id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "mimir",
                        "version": "1.0.0",
                        "workspace": _ws_label,
                    },
                })

            elif method == "tools/list":
                return _response(request_id, {
                    "tools": [
                        {
                            "name": "get_context",
                            "description": (
                                "Retrieve relevant source code context for a natural language query. "
                                "Call this BEFORE answering any question about how the codebase works, "
                                "what a function does, where a feature is implemented, or how components interact. "
                                "Returns a minimal, connected, token-budget-aware context bundle assembled from "
                                "the code graph — including the most relevant functions, classes, and their "
                                "dependencies. "
                                "Use `session_id` to enable cross-turn deduplication: pass the same ID on every "
                                "turn of a conversation so that code already seen by the LLM is summarized or "
                                "omitted in subsequent responses, reducing token usage. "
                                "Use `repos` to restrict results to specific repositories when working in a "
                                "multi-repo workspace. "
                                "Use `budget` to control the maximum token count of the returned context "
                                "(default 8000)."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Natural language question or task description, e.g. 'how does authentication work' or 'where is the retry logic for API calls'",
                                    },
                                    "budget": {
                                        "type": "integer",
                                        "description": "Maximum tokens to include in the context bundle. Default: 8000. Reduce for faster responses or when only a summary is needed.",
                                    },
                                    "repos": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Optional list of repo names to restrict the search to. Omit to search all indexed repos.",
                                    },
                                    "session_id": {
                                        "type": "string",
                                        "description": "Conversation session ID. Pass the same value on every turn to enable deduplication — code sent in a previous turn will be summarized or omitted, reducing repeated context.",
                                    },
                                },
                                "required": ["query"],
                            },
                        },
                        {
                            "name": "get_graph_stats",
                            "description": (
                                "Return statistics about the indexed code graph: total node count, edge count, "
                                "breakdown by node kind (functions, classes, files, modules), and repos indexed. "
                                "Call this when the user asks what has been indexed, how large the codebase is, "
                                "or to confirm that indexing completed successfully before proceeding with queries."
                            ),
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                        {
                            "name": "get_hotspots",
                            "description": (
                                "Return the most recently and frequently modified code nodes, ranked by a "
                                "combined recency + change-frequency score. "
                                "Call this when the user asks about active development areas, what has changed "
                                "recently, where bugs are most likely introduced, or what to focus a code review on. "
                                "Results include node ID, hotspot score, and commit count."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "top_n": {
                                        "type": "integer",
                                        "description": "Number of hotspots to return. Default: 20.",
                                    },
                                },
                            },
                        },
                        {
                            "name": "get_write_context",
                            "description": (
                                "Get everything you need to know before editing a file: the symbols it contains, "
                                "interfaces and base classes those symbols implement or extend, sibling implementations, "
                                "the associated test file, DI/factory registrations, and import relationships. "
                                "Call this BEFORE modifying a file so you understand the contracts, tests, and "
                                "dependents you need to satisfy."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "file_path": {
                                        "type": "string",
                                        "description": "Path to the file you intend to edit, e.g. 'src/auth/login.py' or 'LoginView.swift'. Suffix matching is supported.",
                                    },
                                },
                                "required": ["file_path"],
                            },
                        },
                        {
                            "name": "get_impact",
                            "description": (
                                "Analyze what would be affected if you change a symbol or file. "
                                "Returns direct callers, type users, implementors/subclasses, associated test files, "
                                "and transitive dependencies up to N hops. "
                                "Call this before refactoring, renaming, or removing code to understand the blast radius."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "node_id": {
                                        "type": "string",
                                        "description": "Node ID from a previous get_context result. Preferred if available.",
                                    },
                                    "file_path": {
                                        "type": "string",
                                        "description": "File path to narrow down the search. Used with symbol_name.",
                                    },
                                    "symbol_name": {
                                        "type": "string",
                                        "description": "Name of the function, class, or method to analyze.",
                                    },
                                    "max_hops": {
                                        "type": "integer",
                                        "description": "Maximum transitive dependency depth. Default: 3.",
                                    },
                                },
                            },
                        },
                        {
                            "name": "get_quality",
                            "description": (
                                "Analyze the connectivity quality of the code graph and detect gaps — nodes "
                                "with missing or weak connections that may indicate under-indexed or poorly-resolved "
                                "areas of the codebase. Returns a quality overview with average scores, distribution, "
                                "and a list of the worst-connected nodes with diagnostic reasons. "
                                "Call this to assess index health, find areas that need re-indexing, or understand "
                                "which parts of the codebase have incomplete symbol resolution."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "repos": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Optional list of repo names to restrict analysis to.",
                                    },
                                    "threshold": {
                                        "type": "number",
                                        "description": "Quality score threshold below which nodes are flagged as gaps. Default: 0.3.",
                                    },
                                    "top_n": {
                                        "type": "integer",
                                        "description": "Maximum number of gap nodes to return. Default: 50.",
                                    },
                                },
                            },
                        },
                        {
                            "name": "get_catalog",
                            "description": (
                                "Generate a Backstage-compatible service catalog from the code graph. "
                                "Returns discovered services with their APIs, cross-repo dependencies, "
                                "tech stack, ownership, and quality scores. "
                                "Use `repos` to restrict to specific repositories."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "repos": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Optional list of repo names to include. Omit for all repos.",
                                    },
                                },
                            },
                        },
                        {
                            "name": "get_catalog_drift",
                            "description": (
                                "Compare declared service dependencies against what the code graph actually shows. "
                                "Detects undeclared dependencies (in code but not in catalog) and missing dependencies "
                                "(declared but no code evidence). Returns a drift score from 0 (perfect) to 1 (fully mismatched)."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "repo": {
                                        "type": "string",
                                        "description": "Repository name to check drift for.",
                                    },
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
                                        "description": "List of declared dependencies, each with a 'name' and optional 'type'.",
                                    },
                                },
                                "required": ["repo", "declared_dependencies"],
                            },
                        },
                        {
                            "name": "validate_change",
                            "description": (
                                "Validate a code change against architectural rules before committing. "
                                "Pass a unified diff (from `git diff`) and receive a list of violations "
                                "if any architectural rules are broken. Use this BEFORE committing changes "
                                "to catch layer violations, circular dependencies, coupling threshold "
                                "breaches, and high-impact API changes."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "diff": {
                                        "type": "string",
                                        "description": "Unified diff text (output of `git diff` or `git diff --cached`)",
                                    },
                                    "rules_path": {
                                        "type": "string",
                                        "description": "Path to mimir-rules.yaml. Default: ./mimir-rules.yaml",
                                    },
                                },
                                "required": ["diff"],
                            },
                        },
                        {
                            "name": "can_i_modify",
                            "description": (
                                "Check if you are allowed to modify a file per the agent policy. "
                                "Returns whether the file is within the agent's allowed scope "
                                "and whether human review would be required."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "file_path": {
                                        "type": "string",
                                        "description": "Path to the file you want to modify.",
                                    },
                                    "policy_path": {
                                        "type": "string",
                                        "description": "Path to mimir-agent-policy.yaml. Default: ./mimir-agent-policy.yaml",
                                    },
                                },
                                "required": ["file_path"],
                            },
                        },
                    ],
                })

            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})

                if tool_name == "get_context":
                    bundle = await container.retrieval.search(
                        query=tool_args["query"],
                        graph=graph,
                        token_budget=tool_args.get("budget"),
                        repos=tool_args.get("repos"),
                    )

                    # Session handling
                    session_id = tool_args.get("session_id")
                    if session_id:
                        session = container.session.get_or_create(session_id)
                        sg = _bundle_to_subgraph(bundle)
                        container.session.session_dedup(
                            sg, session, query_embedding=bundle.query_embedding,
                        )

                        # Re-fit to budget after dedup may have changed node set
                        effective_budget = tool_args.get("budget") or container.retrieval._config.retrieval.default_token_budget
                        container.retrieval._fit_to_budget(sg, effective_budget, seed_ids=set())

                        # Apply deduplication back to the bundle
                        bundle.nodes = list(sg.nodes.values())
                        bundle.edges = sg.edges
                        bundle.token_count = sg.token_estimate
                        if sg.notes:
                            bundle.session_note = "Previously seen chunks omitted: " + str(len(sg.notes))

                        container.session.record_retrieval(
                            session,
                            tool_args["query"],
                            bundle.nodes,
                            {n.id: 1.0 for n in bundle.nodes},
                            query_embedding=bundle.query_embedding,
                        )

                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": bundle.format_for_llm(),
                        }],
                    })

                elif tool_name == "get_graph_stats":
                    stats = graph.stats()
                    stats["workspace"] = _ws_label
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(stats, indent=2),
                        }],
                    })

                elif tool_name == "get_hotspots":
                    top_n = tool_args.get("top_n", 20)
                    results = container.temporal.get_hotspots(graph, top_n=top_n)
                    hotspots = [
                        {"node": n.id, "score": f"{s:.3f}", "changes": n.modification_count}
                        for n, s in results
                    ]
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(hotspots, indent=2),
                        }],
                    })

                elif tool_name == "get_write_context":
                    wc = container.write_context.assemble(
                        file_path=tool_args["file_path"],
                        graph=graph,
                    )
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": wc.format_for_llm(),
                        }],
                    })

                elif tool_name == "get_impact":
                    result = container.impact.analyze(
                        graph,
                        node_id=tool_args.get("node_id"),
                        file_path=tool_args.get("file_path"),
                        symbol_name=tool_args.get("symbol_name"),
                        max_hops=tool_args.get("max_hops", 3),
                    )
                    if result is None:
                        text = "No matching symbol or file found for impact analysis."
                    else:
                        text = result.format_for_llm()
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": text,
                        }],
                    })

                elif tool_name == "get_quality":
                    overview = container.quality.detect_gaps(
                        graph,
                        repos=tool_args.get("repos"),
                        threshold=tool_args.get("threshold"),
                        top_n=tool_args.get("top_n", 50),
                    )
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": overview.format_for_llm(),
                        }],
                    })

                elif tool_name == "get_catalog":
                    response = container.catalog.generate_catalog(
                        graph, repos=tool_args.get("repos"),
                    )
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": response.format_for_llm(),
                        }],
                    })

                elif tool_name == "get_catalog_drift":
                    report = container.catalog.detect_drift(
                        graph,
                        repo=tool_args["repo"],
                        declared_deps=tool_args.get("declared_dependencies", []),
                    )
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": report.format_for_llm(),
                        }],
                    })

                elif tool_name == "validate_change":
                    from pathlib import Path
                    from mimir.domain.guardrails_config import load_rules

                    rules_path = Path(tool_args.get("rules_path", "mimir-rules.yaml"))
                    rules = load_rules(rules_path)
                    result = await container.guardrail.evaluate(
                        graph, tool_args["diff"], rules,
                    )
                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": result.format_for_llm(),
                        }],
                    })

                elif tool_name == "can_i_modify":
                    from pathlib import Path
                    from mimir.domain.guardrails_config import load_agent_policy
                    from mimir.services.agent_policy import AgentPolicy

                    policy_path = Path(tool_args.get("policy_path", "mimir-agent-policy.yaml"))
                    try:
                        raw_policies = load_agent_policy(policy_path)
                        policy = AgentPolicy.from_dict(raw_policies[0]) if raw_policies else None
                    except Exception:
                        policy = None

                    file_path = tool_args["file_path"]
                    if policy:
                        allowed = container.agent_policy.check_file_access(policy, file_path)
                        text = (
                            f"File: {file_path}\n"
                            f"Policy: {policy.name}\n"
                            f"Allowed: {'yes' if allowed else 'NO — outside agent scope'}"
                        )
                    else:
                        text = f"File: {file_path}\nNo agent policy found — allowed by default."

                    return _response(request_id, {
                        "content": [{
                            "type": "text",
                            "text": text,
                        }],
                    })

                else:
                    return _error_response(request_id, -32601, f"Unknown tool: {tool_name}")

            elif method == "notifications/initialized":
                return {}  # Acknowledgement, no response needed

            else:
                return _error_response(request_id, -32601, f"Unknown method: {method}")

        except Exception as exc:
            logger.error("MCP request failed: %s", exc, exc_info=True)
            return _error_response(request_id, -32000, str(exc))

    async def main_loop():
        """Read JSON-RPC messages from stdin, write responses to stdout."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        loop = asyncio.get_event_loop()

        # Start file watcher if enabled
        if _watcher_enabled:
            try:
                container.watcher.start(loop)
            except Exception as exc:
                logger.error("Failed to start file watcher: %s", exc)
        write_transport, _ = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )

        buffer = b""
        while True:
            try:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data

                # Try to parse JSON-RPC messages
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                        response = await handle_request(request)
                        if response and response.get("id") is not None:
                            out = json.dumps(response) + "\n"
                            write_transport.write(out.encode())
                    except json.JSONDecodeError:
                        continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("MCP loop error: %s", exc, exc_info=True)

    try:
        asyncio.run(main_loop())
    finally:
        container.close()


def _response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _bundle_to_subgraph(bundle):
    """Convert a ContextBundle back to a SubGraph for session dedup."""
    from mimir.domain.subgraph import SubGraph
    sg = SubGraph()
    for n in bundle.nodes:
        sg.add_node(n)
    for e in bundle.edges:
        sg.add_edge(e)
    return sg
