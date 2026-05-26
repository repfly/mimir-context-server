# Repository Guidelines

## Project Structure & Module Organization
`mimir/` is the main server package. Keep core models in `mimir/domain/`, interfaces in `mimir/ports/`, business logic in `mimir/services/`, concrete implementations in `mimir/infra/`, and external entry points in `mimir/adapters/`. `mimir/container.py` wires dependencies.

`tests/` mirrors the runtime layout with focused suites such as `tests/services/`, `tests/adapters/`, `tests/domain/`, and `tests/infra/`. The lightweight remote client lives in `client/mimir_client/`. The optional Backstage integration is isolated under `backstage-plugin/plugins/catalog-backend-module-mimir/`. Docs and example configs live in `docs/`, `mimir.toml`, `mimir-rules.yaml`, and `mimir-agent-policy.yaml`.

## Build, Test, and Development Commands
Install the server for development with `pip install -e ".[dev]"`. Run the Python test suite with `pytest` or `.venv/bin/pytest` if using the checked-in virtualenv layout. Common local workflows:

- `mimir index` rebuilds the semantic graph for the current workspace.
- `mimir serve` starts the MCP stdio server.
- `mimir serve --http` starts the shared HTTP server on port `8421`.
- `mimir guardrail check` validates the current diff against architectural rules.

For the client package, use `cd client && pip install -e .`. For the Backstage plugin, use `cd backstage-plugin/plugins/catalog-backend-module-mimir && npm test && npm run build`.

## Coding Style & Naming Conventions
Follow the existing hexagonal boundaries; do not let adapters reach into infra directly. Python targets 3.11+ and uses type hints, frozen dataclass-style domain models, and `snake_case` for modules, functions, and variables. Classes and enums use `PascalCase`; constants use `UPPER_SNAKE_CASE`. TypeScript in the Backstage plugin follows the same naming split: `PascalCase` for exported types/classes, `camelCase` for values.

## Testing Guidelines
Add tests next to the affected layer and name files `test_<feature>.py` or `*.test.ts`. Prefer narrow unit tests for services and adapters, then add integration coverage when indexing, retrieval, or HTTP behavior changes. Run `pytest` before opening a PR; run `npm test` in the Backstage plugin when touching `backstage-plugin/`.

## Commit & Pull Request Guidelines
Recent history favors short imperative subjects with prefixes like `add:`, `fix:`, and `update:`. Keep commits focused and descriptive, for example `fix: preserve token budget in retrieval`. PRs should explain behavior changes, list validation steps, link related issues, and include request/response examples or screenshots when HTTP, web UI, or Backstage behavior changes.
