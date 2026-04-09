# How It Works

> [Back to README](../README.md)

## Indexing Pipeline

```
Source files → Tree-sitter parse → Nodes & Edges → CodeGraph (NetworkX)
    → Cross-file symbol resolution (CALLS, USES_TYPE, INHERITS)
    → Heuristic summaries → Structural embedding (code + docstrings + callers + callees + types)
    → Persist to SQLite + VectorStore
```

Each node represents a symbol (function, class, method, module) with its code, signature, docstring, and git metadata.

**Cross-file symbol resolution** scans each symbol's AST for identifiers and matches them against all known symbols in the graph. This produces real dependency edges:

| Edge type | Meaning |
|---|---|
| `CONTAINS` | Parent-child (file → class → method) |
| `CALLS` | Symbol references a function or method in another file |
| `USES_TYPE` | Symbol references a class, struct, protocol, or type |
| `INHERITS` | Class extends or conforms to another class/protocol |
| `IMPORTS` | File-level import relationships |
| `READS_CONFIG` | Symbol reads a configuration key |

## Retrieval Pipeline

1. **Classify intent** — regex pattern matching detects query type (locate, trace, write, debug, general) and selects retrieval parameter profile
2. **Embed query** — single forward pass through the embedding model
3. **Hybrid search** — combines cosine similarity over embeddings, BM25 keyword scoring, and exact/fuzzy name and path matching. Alpha weighting is tuned per intent.
4. **Hierarchical beam search** — containers first, then drill into symbols
5. **Subgraph expansion** — BFS from seeds along typed edges, pruning by relevance gate. Expansion depth adapts per intent (1 hop for locate, 3 for trace).
6. **Type & config context** — include referenced type definitions and config nodes
7. **Quality adjustment** — blend connectivity quality into node scores (85% retrieval + 15% quality), deprioritizing isolated or incomplete nodes
8. **Temporal reranking** — score = 0.45x retrieval + 0.18x recency + 0.12x change frequency + 0.12x co-retrieval + 0.13x quality
9. **Budget fitting** — greedily drop lowest-scoring nodes until token count fits budget
10. **Topological ordering** — order nodes by containment hierarchy for readability

## Session Deduplication

When using `session_id`, Mimir tracks what code has already been sent to the LLM using an exponential decay model:

- **Decay half-life** is `context_decay_turns` (default 5). At the half-life, the decay weight reaches 0.5.
- **Topic similarity bonus**: if the current query is semantically close to the query that originally added a node, the decay weight is boosted (the LLM is more likely to still remember topic-relevant code).
- **Decay weight > 0.8** → omitted (LLM still remembers)
- **0.3 < weight <= 0.8** → summary only (fading from memory)
- **weight <= 0.3** → re-included fully (forgotten)

Co-retrieval learning tracks which nodes appear together and boosts similar nodes in future queries.

## Incremental Indexing

After the initial full index, `mimir index` runs incrementally:

```bash
mimir index                    # first time: full; subsequent: incremental
git pull                       # pull changes
mimir index                    # only re-indexes the diff
mimir index --clean            # force full re-index (wipes existing data)
```

Mimir stores the last-indexed commit hash per repo. On each run:
1. Computes `git diff` against the stored commit
2. Removes stale nodes (deleted/modified files)
3. Re-parses only changed/added files
4. Resolves cross-file references for affected symbols
5. Embeds only new nodes
6. Persists only the delta

Unchanged repos are skipped entirely.

## Live File Watching

For an always-fresh graph during development, Mimir can watch your repos for file changes and re-index automatically:

```bash
mimir serve --watch            # start MCP server with live re-indexing
```

Or enable it permanently in `mimir.toml`:

```toml
[watcher]
enabled = true
```

The file watcher uses a debounced approach — rapid saves (e.g., auto-format on save) are batched into a single re-index operation. Every file save triggers:

1. Tree-sitter re-parse of the changed file
2. Stale node removal + new node creation
3. Affected-set cross-file resolution (only the changed symbols, not the full graph)
4. Heuristic summary generation
5. Embedding + vector store update
6. BM25 index invalidation

The graph, vector store, and search index stay in sync with your code as you edit.

## Quality Scoring & Gap Detection

Every node in the graph carries a **connectivity quality score** in [0, 1], computed from four weighted factors:

| Factor | What it measures |
|---|---|
| **Edge density** | Number of meaningful dependency edges (CALLS, USES_TYPE, etc.) relative to node kind |
| **Embedding presence** | Whether the node has a vector embedding for semantic search |
| **Content completeness** | Whether the node has source code, summary, or docstring |
| **Expected edge coverage** | Fraction of expected edge kinds present (e.g., files should have CONTAINS + IMPORTS) |

Weights are tuned per node kind — edge density matters most for symbols, while structure and content matter more for files.

During retrieval, the quality score is blended into node ranking so that well-connected, fully-resolved nodes are preferred over isolated or incomplete ones. This reduces the chance of surfacing "dead" nodes that lack dependency context.

**Gap detection** scans the graph for nodes below a quality threshold (default 0.3) and diagnoses *why* each node scores poorly:

```bash
mimir quality                        # show overview + worst gaps
mimir quality --threshold 0.5        # stricter threshold
mimir quality --repos my-api         # scope to one repo
```

Typical gap reasons include:
- **Isolated symbol** — a function or class with no dependency edges (not called by or calling anything)
- **Missing embedding** — node was not embedded, invisible to semantic search
- **No code or summary** — empty content, nothing to show in context bundles
- **Missing expected edges** — e.g., a file node with no CONTAINS edges (no symbols extracted)

Gap detection is useful after indexing to verify coverage, or periodically to catch regressions as the codebase evolves.

See also: [Configuration](configuration.md) for tuning parameters, [CLI Reference](cli-reference.md) for `mimir quality` and `mimir index`.
