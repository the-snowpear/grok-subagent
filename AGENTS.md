# Repository Guidelines

## Project Structure & Module Organization

Grok Agent Observer combines a Python MCP bridge with a React viewer. `server.py` is the MCP stdio entry point; `daemon.py` supervises Grok sessions, stores SQLite/FTS events, and serves the local observer. Python regression tests, a deterministic CLI stub, and JSONL fixtures live under `tests/`. The React/TypeScript application is in `viewer/src/`, with Vite configuration in `viewer/vite.config.ts`. Runtime state under `data/` is local and ignored. `viewer/dist/` is a tracked, zero-dependency production fallback; regenerate and commit it after frontend changes.

## Build, Test, and Development Commands

Run Python checks from the repository root:

- `python -m py_compile server.py daemon.py` checks Python syntax.
- `python -m unittest discover -s tests -v` runs the full Python suite.

Run viewer commands from `viewer/`:

- `npm ci` installs the locked frontend dependencies.
- `npm run dev` starts Vite on `127.0.0.1` and proxies `/api` to the observer.
- `npm run test` runs Vitest once.
- `npm run build` type-checks with TypeScript and rebuilds `viewer/dist/`.

No lint or coverage command is currently configured.

## Coding Style & Naming Conventions

Follow existing code style. Python uses four-space indentation, module docstrings, type hints where useful, `snake_case` functions, and `UPPER_SNAKE_CASE` constants. TypeScript uses strict compiler settings, two-space indentation, camelCase helpers, and PascalCase React components. Keep changes focused and preserve comments around security-sensitive path, proxy, and lifecycle behavior.

## Testing Guidelines

Python tests use `unittest`, isolated temporary databases, and fixture-driven cases; name files `test_*.py` and methods `test_<behavior>`. Viewer tests are colocated as `*.test.ts` or `*.test.tsx`. Add regression coverage for behavior changes and run both suites before submitting.

## Commit & Pull Request Guidelines

History currently contains only `Initial migration of Grok Agent Observer`, so no mature convention exists. Use short, imperative, descriptive subjects and keep commits scoped. Pull requests should summarize behavior changes, list verification commands, link relevant issues, and include screenshots for viewer changes. State whether `viewer/dist/` was regenerated and call out security-sensitive changes.

## Security & Configuration Tips

Keep viewer and control services bound to `127.0.0.1`. Never commit `data/`, which may contain prompts, artifacts, process IDs, and local history. Preserve localhost proxy exclusions.
