# Grok Agent Observer

Local Codex MCP bridge and observer for Grok Build CLI.

## Codex plugin

This repository is also a Codex plugin. Its manifest and MCP registration live in `.codex-plugin/plugin.json` and `.mcp.json`. Workflow skills live under `skills/`. The plugin reuses the existing Python MCP bridge and local React viewer; runtime SQLite data remains under ignored `data/` and is not packaged as source content.

The host environment must provide `python` and the `grok` CLI on `PATH`. The MCP server starts from the plugin root, then launches the observer daemon on demand when `create_agent` is called.

### Install in Codex

Prerequisites:

- Python 3 is available as `python` on `PATH`.
- The Grok Build CLI is installed as `grok` on `PATH` and has been authenticated.
- The Codex CLI and ChatGPT desktop app are up to date.

Add this public repository as a plugin marketplace:

```powershell
codex plugin marketplace add thesnowpear/grok-subagent --ref main
```

Alternatively, in the ChatGPT desktop app open **Plugins**, select **Add plugin marketplace**, and enter:

- Source: `thesnowpear/grok-subagent`
- Git reference: `main`
- Sparse path: leave empty

Install the plugin from the marketplace:

```powershell
codex plugin add grok-subagent@grok-subagent
```

Start a new Codex task after installation so the bundled skills and MCP tools are loaded.

To receive repository updates, refresh the marketplace and reinstall the plugin:

```powershell
codex plugin marketplace upgrade grok-subagent
codex plugin add grok-subagent@grok-subagent
```

To uninstall the plugin and remove its marketplace:

```powershell
codex plugin remove grok-subagent
codex plugin marketplace remove grok-subagent
```

### Workflow skills

Codex-side skills that orchestrate the existing `grok` MCP tools (`create_agent`, `send`, `update_agent`, `wait`, `result`, `signoff`, …). Lifecycle and safety defaults live in `skills/grok-delegation/`; scene skills take priority when they match. Skills **do not auto-chain** (e.g. discuss does not auto-start plan; review does not auto-start fix)—the host may *suggest* a next step.

| Skill | Owner | Executor | Codex role | Grok role | Default repo writes |
|-------|-------|----------|------------|-----------|---------------------|
| `grok-delegation` | Codex | Varies | Decompose, verify, integrate | Bounded candidate worker | Allowed when bounded |
| `grok-help` | Codex | Codex | Primary implementer | Temporary advisor | No |
| `grok-discuss` | Codex | None (discussion) | Facilitator | Equal discussant | No |
| `grok-plan` | Codex | Codex (after user OK) | Accept plan, confirm, implement | Plan designer | Not in planning phase |
| `grok-review` | Codex | Codex (adjudicate) | Supply materials, verify findings | Independent reviewer (read-only) | Grok read-only |
| `grok-fix` | Codex | Grok | Issue concrete work orders, accept | Fix-by-ticket | Yes |
| `grok-work` | Grok | Grok | Foreman / supervisor / acceptor | General contractor | Yes |

Hard rules shared by scene skills: tell the user the observer link once after `create_agent`; always `signoff` after local verification when an agent was created; `grok-plan` requires **user confirmation** before Codex implements; `grok-work` must state that work is contracted to Grok with Codex supervising; `grok-review` always asks for the review target unless already specified; `grok-fix` restates verbal findings as a checklist and confirms with the user before coding.

## Behavior

- `server.py` is the MCP stdio entry registered as `grok` in Codex.
- `daemon.py` supervises asynchronous Grok sessions, persists SQLite/FTS events, and serves the local viewer.
- The viewer binds only to `127.0.0.1`, opens on the first `create_agent`, and can be stopped without stopping Grok agents.
- History is retained for seven days from last activity. Large raw output and diffs are gzip-compressed under `data/artifacts`.
- Every Grok process inherits explicit proxy environment variables first; otherwise the daemon reads the enabled Windows user proxy from Internet Settings and maps it to `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`.
- Consecutive streaming text/thought chunks are coalesced before storage. The viewer also groups legacy noisy events and keeps expanded tool/raw payloads inside independently scrollable panels.
- **Changes / edit ledger:** file changes merge (1) paths claimed by write-like tools (`search_replace`, edit titles, …) with (2) workspace snapshot deltas (git or FS). Rows are tagged `claimed` / `observed` / `both`. Parallel agents on the same cwd may mark paths `shared` (ambiguous). Bash-only disk writes appear as observed only.

## Viewer (local UI)

Read-mostly observer for Grok subagents. Live event timeline, FTS search, Changes, and a project → session → agent sidebar.

### Live updates (SSE, event-driven)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/stream?agent_id=…&after=seq` | Per-agent event stream. Wakes on `add_event` / `notify_agent` instead of 1 Hz polling; idle heartbeat about every 15s; reconnects after ~10 minutes. |
| `GET /api/stream/catalog` | Sidebar/list refresh signal. Wakes when any agent or task metadata changes; the UI soft-refreshes bootstrap (throttled ~1.5s). |

A 60s bootstrap poll remains as a fallback if the catalog SSE drops. Prefer keeping one browser tab open so EventSource can reconnect automatically.

### Live streaming telemetry

The MCP plugin now launches `server_live.py`, which points the existing MCP bridge at
`live_streaming.py`. The live launcher reuses the existing daemon implementation but
promotes Grok session-log `agent_message_chunk` and `agent_thought_chunk` updates into
observer `text` / `thought` SSE events. The legacy coalesced stdout events remain as a
race-safe fallback and are only emitted when matching live chunks were not observed.

The viewer exposes a compact top-right telemetry panel:

- cumulative input/output/cache/reasoning usage from Grok headless `streaming-json`
- cache-hit rate and trusted server cost when present
- semantic current-context usage from Grok ACP `x.ai/session/info`
- system prompt, messages, reasoning/overhead, free space, tool definitions,
  Skills/MCP informational rows, auto-compact threshold, turn/tool/compaction counts

Token and context accounting deliberately use different sources. Headless per-response
`usage` events are the cumulative token source; `end.usage` is only a per-turn fallback
when no response usage was seen. The disposable ACP probe does **not** use
`x.ai/session/usage` for lifetime totals because Grok documents that its in-memory usage
ledger resets when a persisted session is resumed in a new agent process.

Environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `GROK_OBSERVER_CONTEXT_TELEMETRY` | `1` | Set `0`/`false`/`no`/`off` to disable the post-turn ACP ContextInfo probe. |
| `GROK_OBSERVER_CONTEXT_PROBE_TIMEOUT` | `8` | ACP probe timeout in seconds (clamped to 2-20). |
| `GROK_OBSERVER_CONTEXT_DEBUG` | unset | When non-empty, write skipped/failed context-probe diagnostics to stderr. |

Operational note: `server.py` reuses an already-running observer daemon. After installing
or switching to this version, restart the Grok Subagent MCP/observer once so the new
`server_live.py` launcher is used.

### Sidebar: pin, archive, titles

| Action | Scope | Notes |
|--------|--------|------|
| **Pin** | Agent or Codex session (task) | Pinned rows sort first within their group. |
| **Archive** | Agent or session | Hidden by default; use the sidebar **归档** chip to show archived items. Preference is stored in `localStorage`. |
| **Rename** | Agent `display_title` or session `title` | Agent labels prefer `display_title`, then `name`. New agents get an auto title from a human-readable `agent_name`, else the first line of the prompt (≤60 chars). |

Persisted on agents: `display_title`, `pinned`, `archived`. Persisted on tasks: `title`, `pinned`, `archived`. Older local DBs migrate these columns on daemon start.

| API | Body fields |
|-----|-------------|
| `POST /api/agents/{id}/meta` | `pinned?`, `archived?`, `display_title?` |
| `POST /api/tasks/{thread_id}/meta` | `pinned?`, `archived?`, `title?` |

Virtual orphan sessions (`_orphan:…`) cannot be edited. Running agents still cannot be deleted from the observer until they finish or are cancelled.

### Steering note (headless Grok)

Headless `grok -p --output-format streaming-json` is **one-way NDJSON** (no stdin control protocol). `update_agent` still interrupts by terminating the child process and starting a replacement turn on the same session (`--resume`); `lossless_interject` is always `false`. Bidirectional ACP (`grok agent stdio`) is a separate integration path, used only by the read-only context telemetry probe described above; it is not used to drive delegated work.

## Trust model

- Control plane and viewer bind **only to `127.0.0.1`** (loopback).
- There is **no authentication** on the control socket or HTTP viewer APIs. Anyone who can reach your local ports can create agents, read prompts/results, and stream events.
- `data/` is sensitive: it may contain full prompts, final results, process IDs, session IDs, and gzipped artifacts. Do not commit or share it.
- Grok child processes run with the same privileges as the daemon user (automatic approval / unrestricted machine access as configured by the operator).

## Ports

| Service | Preferred port | Fallback |
|---------|----------------|----------|
| Control (JSON line protocol) | `47830` | `47830`–`47849` |
| Viewer (HTTP + SSE) | `47831` | `47831`–`47850` |

Actual ports are written to `data/daemon-state.json` after start.

## Concurrency

- **Same working directory:** multiple agents may run in parallel (like parallel subagents). A soft `concurrency_warning` event is recorded; Changes uses the tool edit ledger plus `shared` badges when attribution may overlap.
- **Per conversation:** at most **5** agents with status `queued` or `running` share the same Codex `thread_id` (`CODEX_THREAD_ID` / MCP context). The 6th `create_agent` in that conversation raises until one finishes or is cancelled.
- **Global safety net:** at most **16** active agents across all conversations (override with env).
- Missing `CODEX_THREAD_ID` collapses to thread id `unknown`, so those agents share one per-conversation bucket.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GROK_OBSERVER_FAKE_GROK` | unset | Path to a CLI stand-in (tests use `tests/fake_grok.py`) instead of real `grok`. |
| `GROK_OBSERVER_NO_BROWSER` | unset | Set to `1` to skip opening a browser when the viewer starts. |
| `GROK_OBSERVER_MAX_PER_THREAD` | `5` | Max active (`queued`/`running`) agents per Codex conversation (`thread_id`). |
| `GROK_OBSERVER_MAX_ACTIVE` | `16` | Max active agents across the whole daemon (all conversations). |
| `GROK_OBSERVER_MAX_QUEUE` | `20` | Max depth of a single agent’s in-memory turn queue. |

Env vars for concurrency limits are read when the daemon process starts.

## Recovery

On daemon restart, `recover()`:

1. Finds agents still marked `running` or `queued`.
2. If a durable `child_pid` is recorded and still alive, terminates that orphan Grok process.
3. Marks the agent and its active turns as `failed`, with an error mentioning orphan reaping when applicable.
4. Clears `child_pid`.

In-flight work is never resumed automatically after a restart.

## Diagnostics

- `data/daemon.stderr.log` — append-only stderr from the daemon process started by `server.py` (`_ensure_daemon`). On startup timeout, the MCP bridge includes a tail of this log in the error message.
- `data/daemon-state.json` — live pid and bound ports.
- `data/daemon.lock` — singleton lock file.

## Observer link policy (MCP)

- **Once on create:** successful `create_agent` appends one user+assistant Markdown text item
  `[View Grok execution](viewer_url)` so the host can show a clickable link in the chat.
- **No `resource_link`:** the bridge never returns MCP `content` type `resource_link`, so Codex is less
  likely to collect the observer page under the sidebar “输出 / Outputs” list.
- **No repeat on later tools:** `send`, `update_agent`, `result`, and `signoff` keep only the JSON
  `text` + `structuredContent` (still include `viewer_url` in the JSON for clients that need it).
  Server instructions tell the model to mention the link once after create, not again in later turns
  or the final answer unless the user asks.
- **Client caveat:** hosts may still scrape ordinary Markdown URLs from tool text or model replies;
  the server cannot guarantee the link stays out of every UI surface.

## MCP lifecycle

1. `create_agent` returns an `agent_id` immediately.
2. Call `wait` once to block until a terminal state (up to 300 seconds). Intermediate observer events do not wake Codex; do not poll with `status`.
3. Use `result` for the compact final output; the full trace stays in the viewer.
4. Review and verify locally, then call `signoff`.
5. Use `send` to queue a later turn. Use `update_agent` to steer an existing agent:

   | `mode` | Behavior while a turn is running |
   |--------|-----------------------------------|
   | `auto` (default) | If tools are in-flight → `tool_boundary`; otherwise `immediate`. |
   | `immediate` | Interrupt the current turn and resume the same session with the new prompt. |
   | `tool_boundary` | Register a pending update, wait until in-flight tools complete, then interrupt and resume. If idle (no tools), behaves like `immediate`. On `timeout_seconds` (default 30, range 1–300) falls back to immediate and records `immediate_timeout`. |

   While idle, every mode starts a normal follow-up turn. Each `update_agent` call enqueues an
   **independent replacement turn**; multiple updates are processed **in order** (no silent merge of
   prompts). Observer events: `pending_update`, `update_applied` (includes `mode_used` / trigger
   tool info), and `interjection` (compat). Action response includes `requested_mode`, `mode`
   (`waiting_tool_boundary` | `interrupt_and_resume` | `follow_up`), and always
   `lossless_interject: false`—Grok Build 0.2.93 has no public lossless Interject over ACP.
6. Use `cancel` to permanently terminate the agent.

The user selected automatic approval and unrestricted machine access for Grok. The viewer records this clearly but does not enforce a sandbox.

## Verification

```powershell
python -m py_compile server.py daemon.py
python -m unittest discover -s tests -v
```

Viewer (from `viewer/`):

```powershell
npm ci
npm run test
npm run build
```

The React/TypeScript source is in `viewer/src`. `viewer/dist` is a zero-dependency production fallback so the observer remains usable when npm is unavailable; regenerate and commit it after frontend changes.
