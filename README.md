# Grok Agent Observer

Local Codex MCP bridge and observer for Grok Build CLI.

## Behavior

- `server.py` is the MCP stdio entry registered as `grok` in Codex.
- `daemon.py` supervises asynchronous Grok sessions, persists SQLite/FTS events, and serves the local viewer.
- The viewer binds only to `127.0.0.1`, opens on the first `create_agent`, and can be stopped without stopping Grok agents.
- History is retained for seven days from last activity. Large raw output and diffs are gzip-compressed under `data/artifacts`.
- Every Grok process inherits explicit proxy environment variables first; otherwise the daemon reads the enabled Windows user proxy from Internet Settings and maps it to `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`.
- Consecutive streaming text/thought chunks are coalesced before storage. The viewer also groups legacy noisy events and keeps expanded tool/raw payloads inside independently scrollable panels.

## MCP lifecycle

1. `create_agent` returns an `agent_id` immediately.
2. Call `wait` once to block until a terminal state (up to 300 seconds). Intermediate observer events do not wake Codex; do not poll with `status`.
3. Use `result` for the compact final output; the full trace stays in the viewer.
4. Review and verify locally, then call `signoff`.
5. Use `send` to continue the same Grok conversation or `cancel` to terminate it.

The user selected automatic approval and unrestricted machine access for Grok. The viewer records this clearly but does not enforce a sandbox.

## Verification

```powershell
python -m py_compile server.py daemon.py
python -m unittest discover -s tests -v
```

The React/TypeScript source is in `viewer/src`. `viewer/dist` is a zero-dependency production fallback so the observer remains usable when npm is unavailable.
