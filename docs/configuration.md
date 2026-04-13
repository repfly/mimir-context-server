# Configuration

> [Back to README](../README.md)

Run `mimir init` to generate a template, or create one manually:

```toml
[[repos]]
name = "my-api"
path = "/path/to/my-api"
language_hint = "python"

[[repos]]
name = "my-frontend"
path = "/path/to/my-frontend"
language_hint = "typescript"

[indexing]
summary_mode = "heuristic"   # none | heuristic
excluded_patterns = ["__pycache__", "node_modules", ".git", "venv", ".venv"]
max_file_size_kb = 500
concurrency = 10

[embeddings]
model = "all-mpnet-base-v2"  # local, offline, no API keys needed
batch_size = 64

[vector_db]
backend = "numpy"            # numpy (in-process) | chroma (persistent)

[retrieval]
default_beam_width = 3
default_token_budget = 8000
expansion_hops = 2
hybrid_alpha = 0.7           # balance between semantic and BM25 keyword search
relevance_gate = 0.3

[temporal]
recency_lambda = 0.02
change_window_commits = 100
co_retrieval_enabled = true

[session]
context_decay_turns = 5
topic_tracking_alpha = 0.3

[watcher]
enabled = false              # set to true or use --watch flag
debounce_ms = 1000           # ms to wait after last file event before processing
batch_window_ms = 2000       # max ms to accumulate changes before forcing a flush
```

## Reference

| Section | Key | Default | Description |
|---|---|---|---|
| `indexing` | `summary_mode` | `heuristic` | `none` = raw code only; `heuristic` = signatures, docstrings, and dependency info |
| `indexing` | `max_file_size_kb` | `500` | Skip files larger than this |
| `indexing` | `concurrency` | `10` | Parallel file parsing limit |
| `embeddings` | `model` | `jina-embeddings-v2-base-code` | Any sentence-transformers model or Jina API model |
| `vector_db` | `backend` | `numpy` | `numpy` for dev/small projects; `chroma` for persistent production use |
| `retrieval` | `default_token_budget` | `8000` | Maximum tokens per context bundle |
| `retrieval` | `expansion_hops` | `2` | How many graph hops to expand from seed nodes |
| `retrieval` | `hybrid_alpha` | `0.7` | Weight between semantic (1.0) and BM25 keyword (0.0) search |
| `retrieval` | `relevance_gate` | `0.3` | Minimum score to include expanded nodes |
| `temporal` | `recency_lambda` | `0.02` | Exponential decay rate for recency scoring |
| `session` | `context_decay_turns` | `5` | Turns before previously-sent code is re-included fully |
| `watcher` | `enabled` | `false` | Enable live file watching for automatic re-indexing |
| `watcher` | `debounce_ms` | `1000` | Debounce delay after the last file event |
| `watcher` | `batch_window_ms` | `2000` | Maximum time to accumulate changes before flushing |

See also: [How It Works](how-it-works.md) for what each setting controls, [Docker](docker.md) for environment variable overrides.
