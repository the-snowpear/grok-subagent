"""Persistent Grok process supervisor, event store, and local observer web server."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from coordination import AgentRegistry, CoordinationHub, Mailbox

if os.name == "nt":
    import msvcrt
    import winreg
else:
    import fcntl


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ARTIFACTS = DATA / "artifacts"
STATIC = ROOT / "viewer" / "dist"
DB_PATH = DATA / "observer.sqlite"
STATE_PATH = DATA / "daemon-state.json"
LOCK_PATH = DATA / "daemon.lock"
CONTROL_PORT = 47830
VIEWER_PORT = 47831
RETENTION_DAYS = 7
TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE_TURN = {"queued", "running"}
EVENT_LOCK = threading.Lock()
# Serializes the "check active/queue limits → INSERT" critical sections in
# create_agent/send/update_agent so concurrent control connections cannot race
# past MAX_ACTIVE_* / MAX_QUEUE_DEPTH (ThreadingTCPServer handles each in a thread).
CREATE_LOCK = threading.RLock()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")

# Non-Git workspace snapshot limits (avoid unbounded scans).
FS_SNAPSHOT_MAX_FILES = 8_000
FS_SNAPSHOT_MAX_BYTES = 250 * 1024 * 1024
# Brief wait after Grok stdout so session writers can flush before final drain.
SESSION_FLUSH_WAIT_S = 0.25
SESSION_FINAL_DRAIN_EXTRA_S = 0.15
# Rate-limit monitor error events to avoid storming SQLite / UI.
MONITOR_ERROR_MIN_INTERVAL_S = 2.0

# Concurrency limits (override via env). Same cwd is always allowed; warn only.
# Per-thread = one Codex conversation (agents.thread_id / CODEX_THREAD_ID).
MAX_ACTIVE_PER_THREAD = int(os.environ.get("GROK_OBSERVER_MAX_PER_THREAD", "5"))
MAX_ACTIVE_AGENTS = int(os.environ.get("GROK_OBSERVER_MAX_ACTIVE", "16"))
MAX_QUEUE_DEPTH = int(os.environ.get("GROK_OBSERVER_MAX_QUEUE", "20"))

# Session-log event types that duplicate streaming-json stdout (skip in monitor).
SESSION_SKIP_TYPES = frozenset({
    "phase_changed",
    "loop_started",
    "first_token",
    "reasoning",
    "assistant",
    "user_message_chunk",
    "agent_message_chunk",
    "agent_thought_chunk",
})

# Directory / path noise excluded from non-Git FS diffs (centralized for tests).
FS_EXCLUDE_DIR_NAMES = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "daemon-state",
})
FS_EXCLUDE_FILE_NAMES = frozenset({
    "observer.sqlite",
    "observer.sqlite-wal",
    "observer.sqlite-shm",
    "daemon-state.json",
    "daemon.lock",
})
FS_EXCLUDE_REL_PREFIXES = (
    "data/artifacts/",
    "data/observer.sqlite",
    ".claude/tmp/",
    "viewer/node_modules/",
)
FS_EXCLUDE_REL_EXACT = frozenset({
    "data/daemon-state.json",
    "data/daemon.lock",
    "data/observer.sqlite",
})

# Held for the lifetime of the daemon process so the OS releases it on crash.
_LOCK_HANDLE = None


def clean_terminal_text(value: str) -> str:
    """Remove ANSI styling and non-printing terminal controls from captured logs."""
    value = ANSI_ESCAPE.sub("", value)
    return "".join(char for char in value if char in "\n\r\t" or ord(char) >= 32)


DATA.mkdir(parents=True, exist_ok=True)
ARTIFACTS.mkdir(parents=True, exist_ok=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value, default: int = 0) -> int:
    """Parse a query-string int without letting malformed input crash a handler."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def path_is_within(path: Path, root: Path) -> bool:
    """True iff resolved path is root or a descendant (pathlib boundary, not startswith)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def fully_unquote(value: str, max_rounds: int = 20) -> str:
    """Decode %XX sequences until stable (catches double/triple encoding).

    Loops to a fixed point rather than a fixed count so deeply nested encodings
    are fully resolved; max_rounds only bounds pathological input.
    """
    cur = value
    for _ in range(max_rounds):
        nxt = urllib.parse.unquote(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


def safe_static_relpath(path: str) -> str | None:
    """Normalize a viewer URL path to a relative static key, or None if hostile.

    Rejects parent segments after full URL-decoding (including %252f-style
    double encoding), NUL bytes, backslash separators, and Windows drive paths.
    Returns '' for the site root.
    """
    raw = (path or "/").split("?", 1)[0].split("#", 1)[0]
    decoded = fully_unquote(raw).replace("\\", "/")
    if "\x00" in decoded:
        return None
    # Smuggled absolute Windows path: /C:/Windows/... or C:/Windows/...
    stripped = decoded.lstrip("/")
    if re.match(r"^[A-Za-z]:", stripped):
        return None
    parts: list[str] = []
    for part in decoded.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or part == "~":
            return None
        parts.append(part)
    return "/".join(parts)


def safe_artifact_relpath(rel: str) -> str | None:
    """Normalize /api/artifact?path= values; None means reject."""
    if not rel:
        return None
    decoded = fully_unquote(rel).replace("\\", "/")
    if not decoded or "\x00" in decoded:
        return None
    if decoded.startswith(("/", "\\")) or decoded.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", decoded):
        return None
    parts = Path(decoded).parts
    if ".." in parts or any(p == ".." for p in decoded.split("/")):
        return None
    return decoded


def system_proxy_environment(base: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """Apply the Windows user proxy while preserving explicit proxy env vars."""
    env = base.copy()
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    if any(env.get(name) for name in proxy_names):
        return env, "environment"
    if os.name != "nt":
        return env, None
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
            try:
                bypass = str(winreg.QueryValueEx(key, "ProxyOverride")[0]).strip()
            except FileNotFoundError:
                bypass = ""
        if not enabled or not raw:
            return env, None

        def url(value: str, scheme: str = "http") -> str:
            return value if "://" in value else f"{scheme}://{value}"

        values = {}
        if ";" in raw or "=" in raw:
            for item in raw.split(";"):
                if "=" in item:
                    name, value = item.split("=", 1)
                    values[name.strip().lower()] = value.strip()
        else:
            values = {"http": raw, "https": raw, "all": raw}
        http_proxy = values.get("http") or values.get("https")
        https_proxy = values.get("https") or values.get("http")
        socks_proxy = values.get("socks")
        if http_proxy:
            env.setdefault("HTTP_PROXY", url(http_proxy))
        if https_proxy:
            env.setdefault("HTTPS_PROXY", url(https_proxy))
        if socks_proxy:
            env.setdefault("ALL_PROXY", url(socks_proxy, "socks5"))
        elif http_proxy:
            env.setdefault("ALL_PROXY", url(http_proxy))
        no_proxy = ["127.0.0.1", "localhost"]
        no_proxy.extend(value for value in bypass.replace(";", ",").split(",") if value and value != "<local>")
        env.setdefault("NO_PROXY", ",".join(dict.fromkeys(no_proxy)))
        return env, raw
    except (FileNotFoundError, OSError, ValueError):
        return env, None


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            yield db
    finally:
        # sqlite3.Connection.__exit__ only commits/rolls back; explicitly close it.
        db.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
              thread_id TEXT PRIMARY KEY, title TEXT, cwd TEXT, origin TEXT,
              pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents(
              id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES tasks(thread_id),
              name TEXT NOT NULL, cwd TEXT NOT NULL, grok_session_id TEXT NOT NULL,
              status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              current_turn INTEGER, final_text TEXT DEFAULT '', error TEXT DEFAULT '',
              signoff_verdict TEXT, signoff_summary TEXT, verification TEXT,
              display_title TEXT DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
              archived INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns(
              id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
              turn_no INTEGER NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
              result TEXT DEFAULT '', stop_reason TEXT DEFAULT '',
              created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
              turn_id INTEGER, seq INTEGER NOT NULL, type TEXT NOT NULL, summary TEXT DEFAULT '',
              payload TEXT, artifact_path TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS changes(
              id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
              turn_id INTEGER, path TEXT NOT NULL, kind TEXT, preexisting INTEGER DEFAULT 0,
              added INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, diff_artifact TEXT,
              created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
              agent_id UNINDEXED, kind UNINDEXED, content, tokenize='unicode61'
            );
            CREATE INDEX IF NOT EXISTS idx_events_agent_seq ON events(agent_id, seq);
            CREATE INDEX IF NOT EXISTS idx_agents_thread ON agents(thread_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_turns_agent ON turns(agent_id);
            CREATE INDEX IF NOT EXISTS idx_changes_agent ON changes(agent_id);
            CREATE TABLE IF NOT EXISTS agent_messages(
              id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES tasks(thread_id),
              from_peer TEXT NOT NULL, to_peer TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'message', body TEXT NOT NULL, reply_to TEXT,
              delivery_mode TEXT NOT NULL DEFAULT 'queue', state TEXT NOT NULL DEFAULT 'pending',
              target_turn_id INTEGER, error TEXT, created_at TEXT NOT NULL,
              delivered_at TEXT, consumed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_messages_target_state_created ON agent_messages(to_peer, state, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_thread_created ON agent_messages(thread_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_reply ON agent_messages(reply_to);
            """
        )
        # Best-effort migrations for older local DBs.
        for stmt in (
            "ALTER TABLE agents ADD COLUMN child_pid INTEGER",
            "ALTER TABLE agents ADD COLUMN child_started_at TEXT",
            "ALTER TABLE agents ADD COLUMN display_title TEXT DEFAULT ''",
            "ALTER TABLE agents ADD COLUMN hub_token TEXT",
            "ALTER TABLE agents ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE changes ADD COLUMN source TEXT DEFAULT 'observed'",
            "ALTER TABLE changes ADD COLUMN shared INTEGER DEFAULT 0",
            "ALTER TABLE changes ADD COLUMN tool_name TEXT",
            "ALTER TABLE changes ADD COLUMN tool_call_id TEXT",
        ):
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # Backfill empty display titles from agent name (one-shot, cheap).
        try:
            db.execute(
                "UPDATE agents SET display_title=name "
                "WHERE (display_title IS NULL OR TRIM(display_title)='') AND name IS NOT NULL AND name!=''"
            )
        except sqlite3.OperationalError:
            pass


@contextmanager
def coordination_connect(immediate: bool = False):
    """Open the observer DB for the coordination kernel.

    With immediate=True, runs a BEGIN IMMEDIATE write transaction that commits
    on clean exit and rolls back on exception; otherwise behaves like connect().
    """
    db = sqlite3.connect(DB_PATH, timeout=30)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        if immediate:
            db.isolation_level = None
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()
        else:
            with db:
                yield db
    finally:
        db.close()


def artifact(agent_id: str, label: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
    folder = ARTIFACTS / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{label}-{digest}.txt.gz"
    if not path.exists():
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(content)
    return str(path.relative_to(ROOT))


# Catalog (sidebar) waiters: one global condition; bumped on any agent/task meta change.
CATALOG_CONDITION = threading.Condition()
CATALOG_REVISION = 0
# SSE wait/poll tuning.
SSE_WAIT_TIMEOUT_S = 15.0
SSE_STREAM_MAX_S = 600.0
SSE_CATALOG_HEARTBEAT_S = 25.0


def notify_agent(agent_id: str) -> None:
    """Wake wait() and per-agent SSE streams for this agent."""
    with CONDITIONS_LOCK:
        condition = CONDITIONS.get(agent_id)
    if condition:
        with condition:
            condition.notify_all()
    notify_catalog(agent_id)


def notify_catalog(agent_id: str | None = None) -> None:
    """Wake sidebar/catalog SSE so the viewer can soft-refresh without tight polling."""
    global CATALOG_REVISION
    with CATALOG_CONDITION:
        CATALOG_REVISION += 1
        CATALOG_CONDITION.notify_all()


def derive_display_title(agent_name: str, prompt: str) -> str:
    """Pick a short list title: prefer human agent_name, else first prompt line."""
    name = (agent_name or "").strip()
    # UUID-like names are poor labels in the sidebar.
    uuidish = bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", name))
    if name and not uuidish and name.lower() not in {"agent", "grok", "subagent"}:
        return name[:80]
    line = ""
    for raw in (prompt or "").strip().splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            break
    if not line:
        return (name or "未命名代理")[:80]
    if len(line) > 60:
        return line[:57] + "…"
    return line


def add_event(agent_id: str, turn_id: int | None, event_type: str, summary: str, payload=None) -> int:
    raw = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    search_raw = raw or ""
    artifact_path = None
    if raw and len(raw) > 32_000:
        artifact_path = artifact(agent_id, event_type, raw)
        raw = json.dumps({"truncated": True, "size": len(raw)}, ensure_ascii=False)
    with EVENT_LOCK:
        with connect() as db:
            row = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE agent_id=?", (agent_id,)).fetchone()
            seq = int(row["seq"])
            db.execute(
                "INSERT INTO events(agent_id,turn_id,seq,type,summary,payload,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (agent_id, turn_id, seq, event_type, summary[:2000], raw, artifact_path, now()),
            )
            db.execute("INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)", (agent_id, event_type, summary + "\n" + search_raw))
            db.execute("UPDATE agents SET revision=revision+1,updated_at=? WHERE id=?", (now(), agent_id))
            db.execute("UPDATE tasks SET updated_at=? WHERE thread_id=(SELECT thread_id FROM agents WHERE id=?)", (now(), agent_id))
            revision = db.execute("SELECT revision FROM agents WHERE id=?", (agent_id,)).fetchone()["revision"]
    notify_agent(agent_id)
    return int(revision)


def _file_digest(cwd: Path, rel: str) -> str | None:
    path = cwd / rel
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def parse_porcelain_z(status: str) -> dict[str, dict]:
    """Parse `git status --porcelain=v1 -z` into path -> {xy, rename_from}."""
    entries: dict[str, dict] = {}
    if not status:
        return entries
    parts = status.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        if len(entry) < 3:
            i += 1
            continue
        xy = entry[:2]
        # Porcelain: two status chars, space, then path.
        path = entry[3:] if entry[2:3] == " " else entry[2:].lstrip()
        rename_from = None
        # Rename/copy records a second NUL-terminated original path.
        if "R" in xy or "C" in xy:
            if i + 1 < len(parts):
                rename_from = parts[i + 1] or None
                i += 2
            else:
                i += 1
        else:
            i += 1
        if not path:
            continue
        kind = xy.strip() or "modified"
        if "R" in xy:
            kind = "renamed"
        elif "C" in xy:
            kind = "copied"
        elif "D" in xy or xy == " D":
            kind = "deleted"
        elif "A" in xy or xy == "??":
            kind = "added"
        elif "M" in xy:
            kind = "modified"
        entries[path] = {"xy": xy, "kind": kind, "rename_from": rename_from}
    return entries


def git_snapshot(cwd: Path) -> dict:
    """Capture porcelain status, digests of dirty paths, and diff stats for a turn boundary."""
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=True,
            creationflags=NO_WINDOW,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=True,
            creationflags=NO_WINDOW,
        ).stdout
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--unified=3", "HEAD"],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
            creationflags=NO_WINDOW,
        ).stdout
        numstat = subprocess.run(
            ["git", "diff", "--numstat", "HEAD"],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=NO_WINDOW,
        ).stdout
        entries = parse_porcelain_z(status)
        digests = {path: _file_digest(cwd, path) for path in entries}
        return {
            "available": True,
            "mode": "git",
            "root": root,
            "status": status,
            "entries": entries,
            "digests": digests,
            "diff": diff,
            "numstat": numstat,
        }
    except Exception:
        return {
            "available": False,
            "mode": "git",
            "entries": {},
            "digests": {},
            "status": "",
            "diff": "",
            "numstat": "",
        }


def fs_path_excluded(rel: str) -> bool:
    """True if a cwd-relative path should be ignored by non-Git snapshot diffs."""
    # Do not use lstrip("./") — that strips any leading '.' chars and breaks ".claude/...".
    norm = rel.replace("\\", "/").removeprefix("./")
    while norm.startswith("/"):
        norm = norm[1:]
    if not norm:
        return True
    if norm in FS_EXCLUDE_REL_EXACT:
        return True
    for prefix in FS_EXCLUDE_REL_PREFIXES:
        if norm == prefix.rstrip("/") or norm.startswith(prefix):
            return True
    parts = norm.split("/")
    if any(part in FS_EXCLUDE_DIR_NAMES for part in parts[:-1] if part):
        return True
    name = parts[-1] if parts else ""
    if name in FS_EXCLUDE_FILE_NAMES:
        return True
    # viewer/dist sourcemaps are build noise; keep JS/CSS diffs.
    if norm.startswith("viewer/dist/") and name.endswith(".map"):
        return True
    return False


def fs_snapshot(cwd: Path, prior: dict | None = None) -> dict:
    """Strict per-turn filesystem snapshot: rel path -> size/mtime_ns/sha256.

    When `prior` is a previous fs snapshot, files whose (size, mtime_ns) are
    unchanged reuse the recorded sha256 instead of being re-hashed — safe because
    a content change bumps mtime, and it roughly halves per-turn hashing cost.
    """
    entries: dict[str, dict] = {}
    digests: dict[str, str | None] = {}
    total_bytes = 0
    prior_entries: dict[str, dict] = (prior or {}).get("entries") or {}
    prior_digests: dict[str, str | None] = (prior or {}).get("digests") or {}
    try:
        root = cwd.resolve()
    except OSError as exc:
        return {
            "available": False,
            "mode": "fs",
            "reason": "changes_unavailable",
            "error": str(exc),
            "entries": {},
            "digests": {},
        }

    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            try:
                rel_dir = Path(dirpath).resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                dirnames[:] = []
                continue
            if rel_dir == ".":
                rel_dir = ""
            # Prune excluded directories in-place.
            kept: list[str] = []
            for name in dirnames:
                child = f"{rel_dir}/{name}" if rel_dir else name
                if name in FS_EXCLUDE_DIR_NAMES or fs_path_excluded(child):
                    continue
                kept.append(name)
            dirnames[:] = kept

            for name in filenames:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                rel = rel.replace("\\", "/")
                if fs_path_excluded(rel):
                    continue
                path = Path(dirpath) / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    st = path.stat()
                except OSError:
                    continue
                size = int(st.st_size)
                total_bytes += size
                if len(entries) + 1 > FS_SNAPSHOT_MAX_FILES or total_bytes > FS_SNAPSHOT_MAX_BYTES:
                    return {
                        "available": False,
                        "mode": "fs",
                        "reason": "changes_unavailable",
                        "error": "workspace too large for non-git snapshot",
                        "limit_files": FS_SNAPSHOT_MAX_FILES,
                        "limit_bytes": FS_SNAPSHOT_MAX_BYTES,
                        "entries": {},
                        "digests": {},
                    }
                mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
                prior_meta = prior_entries.get(rel)
                if (
                    prior_meta is not None
                    and prior_meta.get("size") == size
                    and prior_meta.get("mtime_ns") == mtime_ns
                    and rel in prior_digests
                ):
                    # Unchanged since the prior snapshot — reuse its hash, skip re-read.
                    digest = prior_digests[rel]
                else:
                    digest = _file_digest(root, rel)
                meta = {
                    "kind": "present",
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "sha256": digest,
                }
                entries[rel] = meta
                digests[rel] = digest
    except OSError as exc:
        return {
            "available": False,
            "mode": "fs",
            "reason": "changes_unavailable",
            "error": str(exc),
            "entries": {},
            "digests": {},
        }

    return {
        "available": True,
        "mode": "fs",
        "root": str(root),
        "entries": entries,
        "digests": digests,
        "file_count": len(entries),
        "total_bytes": total_bytes,
    }


def workspace_snapshot(cwd: Path, prior: dict | None = None) -> dict:
    """Prefer Git incremental snapshot; fall back to non-Git FS snapshot (never init Git)."""
    git = git_snapshot(cwd)
    if git.get("available"):
        git = dict(git)
        git["mode"] = "git"
        return git
    # Only fs snapshots carry a reusable digest cache; ignore a git-mode prior.
    fs_prior = prior if prior and prior.get("mode") == "fs" else None
    return fs_snapshot(cwd, prior=fs_prior)


# ── Tool edit ledger (claimed paths from write-like tools) ─────────────────

WRITE_TOOL_NAMES = frozenset({
    "search_replace",
    "str_replace",
    "write",
    "write_file",
    "create_file",
    "delete_file",
    "apply_patch",
    "edit",
    "edit_file",
})
READ_ONLY_TOOL_NAMES = frozenset({
    "read_file",
    "read",
    "list_dir",
    "glob",
    "grep",
    "search",
    "web_search",
    "run_terminal_command",
    "bash",
})
TITLE_PATH_RE = re.compile(
    r"(?i)^(?:edit|write|create|delete|update)\s+`([^`]+)`",
)
TOOL_EVENT_TYPES = frozenset({"tool_call", "tool_call_update"})


def normalize_edit_path(path: str) -> str:
    """Normalize a tool-reported path for the changes ledger."""
    value = (path or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _pick_str(obj: dict, *keys: str) -> str | None:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _tool_update_root(payload: dict) -> dict:
    """Locate the tool update object inside session JSON or flattened storage."""
    if not isinstance(payload, dict):
        return {}
    params = payload.get("params") if isinstance(payload.get("params"), dict) else None
    if params and isinstance(params.get("update"), dict):
        return params["update"]
    if isinstance(payload.get("update"), dict):
        return payload["update"]
    # Already a flattened tool_call-like dict (or streaming-json wrap).
    return payload


def _tool_identity(payload: dict) -> tuple[str, str, str, str | None]:
    """Return (tool_name, title, kind_meta, tool_call_id) from mixed payload shapes."""
    update = _tool_update_root(payload)
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
    tool_meta = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), dict) else {}
    name = (
        _pick_str(tool_meta, "name", "label")
        or _pick_str(update, "name", "tool", "toolName", "tool_name")
        or _pick_str(payload, "name", "tool", "toolName")
        or ""
    )
    title = _pick_str(update, "title") or _pick_str(payload, "title") or ""
    kind = str(tool_meta.get("kind") or update.get("kind") or payload.get("kind") or "").lower()
    call_id = (
        _pick_str(update, "toolCallId", "tool_call_id", "callId", "id")
        or _pick_str(payload, "toolCallId", "tool_call_id", "callId")
    )
    return name.lower(), title, kind, call_id


def is_write_like_tool(tool_name: str, title: str, kind: str) -> bool:
    """True when this tool should contribute a claimed edit path."""
    name = (tool_name or "").lower().strip()
    if name in READ_ONLY_TOOL_NAMES:
        return False
    if name in WRITE_TOOL_NAMES:
        return True
    if kind == "edit":
        return True
    if any(tok in name for tok in ("search_replace", "str_replace", "apply_patch", "write_file", "delete_file")):
        return True
    if TITLE_PATH_RE.match((title or "").strip()):
        return True
    return False


def _paths_from_mapping(mapping: dict) -> list[str]:
    found: list[str] = []
    for key in ("file_path", "path", "filePath", "target_file"):
        val = mapping.get(key)
        if isinstance(val, str) and val.strip():
            found.append(val.strip())
    for nest_key in ("EditsApplied", "FileContent", "Write", "Delete"):
        nested = mapping.get(nest_key)
        if isinstance(nested, dict):
            p = _pick_str(nested, "path", "file_path", "filePath")
            if p:
                found.append(p)
    return found


def extract_edit_claims_from_payload(payload) -> list[dict]:
    """
    Extract claimed write paths from a stored tool event payload.
    Returns list of {path, tool_name, tool_call_id, kind}.
    """
    if not isinstance(payload, dict):
        return []
    # Session lines store full JSONL objects; dig into params.update when present.
    update = _tool_update_root(payload)
    if not isinstance(update, dict) or not update:
        return []

    tool_name, title, kind_meta, call_id = _tool_identity(payload)
    if not is_write_like_tool(tool_name, title, kind_meta):
        return []

    candidates: list[str] = []
    for blob in (
        update.get("rawInput"),
        update.get("rawOutput"),
        update.get("input"),
        update.get("arguments"),
        payload.get("rawInput"),
        payload.get("rawOutput"),
        payload.get("input"),
        payload.get("arguments"),
        payload.get("data") if isinstance(payload.get("data"), dict) else None,
    ):
        if isinstance(blob, dict):
            candidates.extend(_paths_from_mapping(blob))

    m = TITLE_PATH_RE.match(title.strip()) if title else None
    if m:
        candidates.append(m.group(1))

    # Infer kind for claimed-only rows.
    claim_kind = "modified"
    title_l = title.lower()
    if "delete" in tool_name or title_l.startswith("delete"):
        claim_kind = "deleted"
    elif "create" in tool_name or tool_name in {"write", "write_file"} or title_l.startswith(("write", "create")):
        claim_kind = "added"

    results: list[dict] = []
    seen: set[str] = set()
    for raw in candidates:
        path = normalize_edit_path(raw)
        if not path or path in seen:
            continue
        # Skip absurd absolute Windows paths that are clearly not repo-relative? still keep.
        seen.add(path)
        results.append({
            "path": path,
            "tool_name": tool_name or None,
            "tool_call_id": call_id,
            "kind": claim_kind,
        })
    return results


def collect_claimed_edits(agent_id: str, turn_id: int) -> dict[str, dict]:
    """Scan this turn's tool events → path -> claim meta (last write wins for tool_name)."""
    claims: dict[str, dict] = {}
    with connect() as db:
        rows = db.execute(
            "SELECT type, payload FROM events WHERE agent_id=? AND turn_id=? "
            "AND type IN ('tool_call','tool_call_update') ORDER BY seq",
            (agent_id, turn_id),
        ).fetchall()
    for row in rows:
        raw = row["payload"]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for claim in extract_edit_claims_from_payload(payload):
            claims[claim["path"]] = claim
    return claims


def _compute_git_observed(before: dict, after: dict) -> tuple[dict[str, dict], str | None]:
    """Return (path -> {kind, preexisting, display_path, added, deleted}, diff_ref_text)."""
    before_entries: dict[str, dict] = before.get("entries") or parse_porcelain_z(before.get("status", ""))
    after_entries: dict[str, dict] = after.get("entries") or parse_porcelain_z(after.get("status", ""))
    before_digests: dict[str, str | None] = before.get("digests") or {}
    after_digests: dict[str, str | None] = after.get("digests") or {}

    stats: dict[str, tuple[int, int]] = {}
    for line in after.get("numstat", "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            stats[parts[2]] = (
                int(parts[0]) if parts[0].isdigit() else 0,
                int(parts[1]) if parts[1].isdigit() else 0,
            )

    observed: dict[str, dict] = {}
    for path, meta in after_entries.items():
        prior = before_entries.get(path)
        if prior is None:
            kind = meta.get("kind") or "modified"
            preexisting = 0
        else:
            changed = (
                prior.get("xy") != meta.get("xy")
                or prior.get("rename_from") != meta.get("rename_from")
                or before_digests.get(path) != after_digests.get(path)
            )
            if not changed:
                continue
            kind = meta.get("kind") or "modified"
            preexisting = 1
        rename_from = meta.get("rename_from")
        display = f"{rename_from} → {path}" if rename_from and kind == "renamed" else path
        added, deleted = stats.get(path, (0, 0))
        observed[path] = {
            "kind": kind,
            "preexisting": preexisting,
            "display_path": display,
            "added": added,
            "deleted": deleted,
        }

    for path, meta in before_entries.items():
        if path in after_entries:
            continue
        observed[path] = {
            "kind": "deleted",
            "preexisting": 1,
            "display_path": path,
            "added": 0,
            "deleted": 0,
        }

    diff_text = after.get("diff") or ""
    return observed, diff_text if diff_text else None


def _compute_fs_observed(before: dict, after: dict) -> dict[str, dict]:
    before_entries: dict[str, dict] = before.get("entries") or {}
    after_entries: dict[str, dict] = after.get("entries") or {}
    before_digests: dict[str, str | None] = before.get("digests") or {}
    after_digests: dict[str, str | None] = after.get("digests") or {}

    observed: dict[str, dict] = {}
    for path, meta in after_entries.items():
        prior = before_entries.get(path)
        if prior is None:
            observed[path] = {
                "kind": "added",
                "preexisting": 0,
                "display_path": path,
                "added": 0,
                "deleted": 0,
            }
            continue
        if before_digests.get(path) != after_digests.get(path):
            observed[path] = {
                "kind": "modified",
                "preexisting": 1 if prior else 0,
                "display_path": path,
                "added": 0,
                "deleted": 0,
            }
        elif before_digests.get(path) is None and after_digests.get(path) is None:
            if prior.get("size") != meta.get("size"):
                observed[path] = {
                    "kind": "modified",
                    "preexisting": 1,
                    "display_path": path,
                    "added": 0,
                    "deleted": 0,
                }

    for path in before_entries:
        if path not in after_entries:
            observed[path] = {
                "kind": "deleted",
                "preexisting": 1,
                "display_path": path,
                "added": 0,
                "deleted": 0,
            }
    return observed


def concurrent_cwd_peer_ids(agent_id: str) -> list[str]:
    """Other agents on the same cwd that are still queued/running."""
    with connect() as db:
        row = db.execute("SELECT cwd FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            return []
        peers = db.execute(
            "SELECT id FROM agents WHERE cwd=? AND id!=? AND status IN ('queued','running')",
            (row["cwd"], agent_id),
        ).fetchall()
    return [r["id"] for r in peers]


def peer_claimed_paths(peer_ids: list[str]) -> set[str]:
    """Paths other agents already recorded as claimed/both (for shared marking)."""
    if not peer_ids:
        return set()
    placeholders = ",".join("?" * len(peer_ids))
    with connect() as db:
        # source column may be null on very old rows → treat as observed only.
        rows = db.execute(
            f"SELECT DISTINCT path FROM changes WHERE agent_id IN ({placeholders}) "
            f"AND (source IN ('claimed','both') OR tool_name IS NOT NULL)",
            peer_ids,
        ).fetchall()
    return {normalize_edit_path(r["path"]) for r in rows if r["path"]}


def record_changes(agent_id: str, turn_id: int, before: dict, after: dict) -> int:
    """
    Merge tool edit ledger (claimed) with workspace snapshot delta (observed).
    Parallel same-cwd peers mark ambiguous paths as shared.
    """
    claimed = collect_claimed_edits(agent_id, turn_id)
    peers = concurrent_cwd_peer_ids(agent_id)
    peer_claims = peer_claimed_paths(peers)
    has_peers = bool(peers)

    observed: dict[str, dict] = {}
    diff_text: str | None = None
    mode = "unavailable"
    snapshot_error: str | None = None

    after_mode = after.get("mode") or ("git" if after.get("available") else None)
    before_mode = before.get("mode") or ("git" if before.get("available") else None)

    if after.get("available") and after.get("mode") == "fs":
        if before.get("available") and before.get("mode") == "fs":
            observed = _compute_fs_observed(before, after)
            mode = "fs"
        else:
            snapshot_error = before.get("error") or "missing start snapshot for non-git workspace"
            mode = "fs"
    elif after.get("available") and (after.get("mode") == "git" or after.get("available")):
        if after.get("mode") == "git" or before.get("available"):
            # Git path: allow empty before (fresh dirty) via existing logic.
            observed, diff_text = _compute_git_observed(before, after)
            mode = "git"
        else:
            snapshot_error = after.get("error") or "changes_unavailable"
    else:
        snapshot_error = after.get("error") or before.get("error") or "changes_unavailable"
        mode = after_mode or before_mode or "unavailable"

    all_paths = set(claimed) | set(observed)
    if not all_paths:
        if snapshot_error and not claimed:
            add_event(
                agent_id,
                turn_id,
                "changes",
                "工作区变更检测不可用" if not claimed else "本轮无变更",
                {
                    "available": False,
                    "count": 0,
                    "reason": "changes_unavailable",
                    "error": snapshot_error,
                    "mode": mode,
                    "claimed": 0,
                    "observed": 0,
                },
            )
            return 0
        add_event(
            agent_id,
            turn_id,
            "changes",
            "本轮无工作区增量变更",
            {"count": 0, "mode": mode, "claimed": 0, "observed": 0},
        )
        return 0

    diff_ref = artifact(agent_id, "git-diff", diff_text) if diff_text else None
    stamp = now()
    claimed_n = observed_n = shared_n = 0

    with connect() as db:
        for path in sorted(all_paths):
            c = claimed.get(path)
            o = observed.get(path)
            if c and o:
                source = "both"
                claimed_n += 1
                observed_n += 1
            elif c:
                source = "claimed"
                claimed_n += 1
            else:
                source = "observed"
                observed_n += 1

            # Shared: peers active and (observed-only OR peer also claimed this path).
            shared = 0
            if has_peers:
                if source == "observed":
                    shared = 1
                elif path in peer_claims:
                    shared = 1
            if shared:
                shared_n += 1

            if o:
                kind = o["kind"]
                preexisting = int(o.get("preexisting") or 0)
                display_path = o.get("display_path") or path
                added = int(o.get("added") or 0)
                deleted = int(o.get("deleted") or 0)
                row_diff = diff_ref
            else:
                kind = (c or {}).get("kind") or "modified"
                preexisting = 0
                display_path = path
                added = 0
                deleted = 0
                row_diff = None

            tool_name = (c or {}).get("tool_name")
            tool_call_id = (c or {}).get("tool_call_id")

            try:
                db.execute(
                    "INSERT INTO changes(agent_id,turn_id,path,kind,preexisting,added,deleted,"
                    "diff_artifact,created_at,source,shared,tool_name,tool_call_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        agent_id,
                        turn_id,
                        display_path,
                        kind,
                        preexisting,
                        added,
                        deleted,
                        row_diff,
                        stamp,
                        source,
                        shared,
                        tool_name,
                        tool_call_id,
                    ),
                )
            except sqlite3.OperationalError:
                # Pre-migration DB fallback without new columns.
                db.execute(
                    "INSERT INTO changes(agent_id,turn_id,path,kind,preexisting,added,deleted,"
                    "diff_artifact,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        agent_id,
                        turn_id,
                        display_path,
                        kind,
                        preexisting,
                        added,
                        deleted,
                        row_diff,
                        stamp,
                    ),
                )
            db.execute(
                "INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)",
                (agent_id, "change", path),
            )

    count = len(all_paths)
    summary = f"工具编辑 {claimed_n} · 工作区观测 {observed_n}"
    if shared_n:
        summary += f" · 交叉 {shared_n}"
    add_event(
        agent_id,
        turn_id,
        "changes",
        summary,
        {
            "count": count,
            "paths": sorted(all_paths),
            "mode": mode,
            "claimed": claimed_n,
            "observed": observed_n,
            "shared": shared_n,
            "peers": peers,
            "snapshot_error": snapshot_error,
        },
    )
    return count


def grok_session_dir(cwd: Path, session_id: str) -> Path:
    encoded = urllib.parse.quote(str(cwd), safe="")
    return Path.home() / ".grok" / "sessions" / encoded / session_id


def session_log_paths(folder: Path) -> list[Path]:
    paths = [folder / "updates.jsonl", folder / "events.jsonl", folder / "chat_history.jsonl"]
    terminal = folder / "terminal"
    if terminal.exists():
        try:
            paths.extend(sorted(terminal.glob("*.log")))
        except OSError:
            pass
    return paths


def _file_head_fingerprint(path: Path, size: int, head_bytes: int = 256) -> str:
    """Stable fingerprint of the file prefix — changes when content is rewritten in place."""
    if size <= 0:
        return ""
    try:
        with path.open("rb") as handle:
            head = handle.read(min(head_bytes, size))
        return hashlib.sha256(head).hexdigest()
    except OSError:
        return ""


def capture_session_log_baseline(cwd: Path, session_id: str) -> dict[str, dict]:
    """Sync snapshot of existing session log sizes before Grok process start.

    Resume turns must start reading from these offsets so history is not replayed.
    Identity (size + mtime_ns + head fingerprint) detects truncation/rotation/rewrite.
    """
    folder = grok_session_dir(cwd, session_id)
    baseline: dict[str, dict] = {}
    for path in session_log_paths(folder):
        key = str(path)
        try:
            if not path.exists() or not path.is_file():
                continue
            st = path.stat()
            size = int(st.st_size)
            baseline[key] = {
                "offset": size,
                "size": size,
                "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
                "head_fp": _file_head_fingerprint(path, size),
            }
        except OSError:
            continue
    return baseline


def _is_byte_array(value: list) -> bool:
    if not value:
        return False
    # Heuristic: list of small ints looks like a byte buffer from Grok rawOutput.
    sample = value[:64]
    return all(isinstance(x, int) and 0 <= x <= 255 for x in sample)


def decode_byte_array(value: list, limit: int = 64_000) -> str:
    data = bytes(int(x) & 0xFF for x in value[:limit])
    text = data.decode("utf-8", errors="replace")
    if len(value) > limit:
        text += f"\n…[truncated {len(value) - limit} bytes]"
    return text


def extract_text_content(value, *, depth: int = 0, limit: int = 4_000) -> str:
    """Recursively pull a human-readable summary from dict/list/string/null content."""
    if value is None or depth > 8:
        return ""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)[:limit]
    if isinstance(value, list):
        if _is_byte_array(value):
            return decode_byte_array(value, limit=limit)[:limit]
        parts: list[str] = []
        size = 0
        for item in value:
            piece = extract_text_content(item, depth=depth + 1, limit=limit - size)
            if not piece:
                continue
            parts.append(piece)
            size += len(piece)
            if size >= limit:
                break
        return "\n".join(parts)[:limit]
    if isinstance(value, dict):
        for key in (
            "text",
            "title",
            "summary",
            "message",
            "output_for_prompt",
            "content",
            "FileContent",
            "EditsApplied",
            "Result",
            "stdout",
            "stderr",
        ):
            if key in value:
                piece = extract_text_content(value.get(key), depth=depth + 1, limit=limit)
                if piece:
                    return piece[:limit]
        # Nested tool payloads often store bytes under "output".
        if "output" in value:
            piece = extract_text_content(value.get("output"), depth=depth + 1, limit=limit)
            if piece:
                return piece[:limit]
        parts = []
        size = 0
        for item in value.values():
            piece = extract_text_content(item, depth=depth + 1, limit=limit - size)
            if not piece:
                continue
            parts.append(piece)
            size += len(piece)
            if size >= limit:
                break
        return "\n".join(parts)[:limit]
    return str(value)[:limit]


def normalize_for_storage(value, *, depth: int = 0):
    """Convert rawOutput byte arrays to text so payloads stay readable and smaller."""
    if depth > 10:
        return value
    if isinstance(value, list):
        if _is_byte_array(value):
            return decode_byte_array(value)
        return [normalize_for_storage(v, depth=depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: normalize_for_storage(v, depth=depth + 1) for k, v in value.items()}
    return value


def session_event_id(obj: dict) -> str | None:
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    event_id = meta.get("eventId")
    if isinstance(event_id, str) and event_id:
        return event_id
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    umeta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
    event_id = umeta.get("eventId")
    if isinstance(event_id, str) and event_id:
        return event_id
    return None


def summarize_session_update(obj: dict, path_name: str) -> tuple[str, str, dict]:
    """Return (event_type, summary, normalized_payload) for a session JSON line."""
    method = obj.get("method", "") if isinstance(obj, dict) else ""
    params = obj.get("params") if isinstance(obj, dict) and isinstance(obj.get("params"), dict) else {}
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    if not isinstance(update, dict):
        update = {}
    event_type = (
        update.get("sessionUpdate")
        or (obj.get("type") if isinstance(obj, dict) else None)
        or method
        or path_name
    )
    event_type = str(event_type or path_name)

    title = update.get("title")
    summary = ""
    if isinstance(title, str) and title.strip():
        summary = title.strip()
    if not summary:
        summary = extract_text_content(update.get("content"))
    if not summary and update.get("rawOutput") is not None:
        summary = extract_text_content(update.get("rawOutput"))
    if not summary and update.get("rawInput") is not None:
        summary = extract_text_content(update.get("rawInput"))
    if not summary:
        tool = None
        meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
        tool_meta = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), dict) else {}
        tool = tool_meta.get("name") or tool_meta.get("label")
        if isinstance(tool, str) and tool:
            summary = tool
    if not summary:
        summary = str(obj.get("type") or method or path_name)

    # Keep full structure for frontend ToolStep, but normalize heavy byte arrays.
    payload = normalize_for_storage(obj) if isinstance(obj, dict) else obj
    return event_type, str(summary)[:2000], payload if isinstance(payload, dict) else {"value": payload}


def content_fingerprint(text: str) -> str:
    cleaned = clean_terminal_text(text or "").strip()
    if not cleaned:
        return ""
    # Normalize whitespace for soft terminal/tool-output dedup.
    collapsed = re.sub(r"\s+", " ", cleaned)
    return hashlib.sha256(collapsed.encode("utf-8", errors="replace")).hexdigest()


class SessionMonitorState:
    """Per-turn cursor + dedup state shared across poll / final drain."""

    def __init__(self, baseline: dict[str, dict] | None = None):
        self.offsets: dict[str, int] = {}
        self.identities: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns) at last open
        # Prefix fingerprint — detects in-place rewrite when new size >= old size.
        self.head_fps: dict[str, str] = {}
        self.seen_event_ids: set[str] = set()
        # Soft dedup for terminal log lines that duplicate tool rawOutput text.
        self.seen_output_fps: set[str] = set()
        self.error_count = 0
        self.last_error_at = 0.0
        self.fatal: str | None = None
        for key, meta in (baseline or {}).items():
            try:
                off = int(meta.get("offset", 0))
                size = int(meta.get("size", off))
                mtime_ns = int(meta.get("mtime_ns", 0))
            except (TypeError, ValueError):
                continue
            self.offsets[key] = max(0, off)
            self.identities[key] = (size, mtime_ns)
            head_fp = meta.get("head_fp")
            if isinstance(head_fp, str) and head_fp:
                self.head_fps[key] = head_fp


def _resolve_read_offset(state: SessionMonitorState, path: Path, size: int, mtime_ns: int) -> int:
    """Handle create / grow / truncate / rotate / in-place rewrite; never seek past EOF."""
    key = str(path)
    prev = state.offsets.get(key)
    prev_id = state.identities.get(key)
    if prev is None:
        # New file this turn: read from start (baseline paths are already in offsets).
        offset = 0
    else:
        offset = prev
        # Truncation or rotation: identity size shrank or offset beyond EOF.
        if offset > size:
            offset = 0
        elif prev_id is not None:
            prev_size, _prev_mtime = prev_id
            if size < prev_size:
                offset = 0
        # In-place rewrite (e.g. write_text replace) can grow past the old cursor while
        # changing the prefix — detect via head fingerprint without replaying pure appends.
        if offset > 0 and size > 0:
            known_head = state.head_fps.get(key)
            if known_head:
                current_head = _file_head_fingerprint(path, size)
                if current_head and current_head != known_head:
                    offset = 0
    offset = max(0, min(offset, size))
    state.identities[key] = (size, mtime_ns)
    if size > 0 and (offset == 0 or key not in state.head_fps):
        fp = _file_head_fingerprint(path, size)
        if fp:
            state.head_fps[key] = fp
    elif size == 0:
        state.head_fps.pop(key, None)
    return offset


def _ingest_session_line(
    agent_id: str,
    turn_id: int,
    path: Path,
    text: str,
    state: SessionMonitorState,
) -> bool:
    """Ingest one complete log line. Returns True if an event was stored."""
    if not text:
        return False
    try:
        if path.suffix == ".log":
            text = clean_terminal_text(text)
            fp = content_fingerprint(text)
            if fp and fp in state.seen_output_fps:
                return False
            if fp:
                state.seen_output_fps.add(fp)
            add_event(
                agent_id,
                turn_id,
                "tool_output",
                text[:2000],
                {"file": path.name, "text": text, "source": "terminal_log"},
            )
            return True

        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            add_event(agent_id, turn_id, "raw", text[:500], {"source": path.name, "text": text})
            return True

        if not isinstance(obj, dict):
            add_event(agent_id, turn_id, "raw", text[:500], {"source": path.name, "text": text})
            return True

        if path.name == "chat_history.jsonl" and obj.get("type") in {"system", "user"}:
            return False

        event_id = session_event_id(obj)
        if event_id and event_id in state.seen_event_ids:
            return False

        event_type, summary, payload = summarize_session_update(obj, path.name)
        if event_type in SESSION_SKIP_TYPES:
            if event_id:
                state.seen_event_ids.add(event_id)
            return False

        # Soft-dedup tool output text against terminal logs (content fingerprint).
        if event_type in {"tool_call_update", "tool_result", "tool_output", "tool_completed"}:
            update = {}
            if isinstance(payload, dict):
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                update = params.get("update") if isinstance(params.get("update"), dict) else {}
            out_text = extract_text_content(update.get("rawOutput")) or extract_text_content(update.get("content"))
            fp = content_fingerprint(out_text) if out_text else ""
            if fp:
                state.seen_output_fps.add(fp)

        add_event(agent_id, turn_id, str(event_type), summary, payload)
        notify_runner_tool_activity(agent_id, turn_id, str(event_type), summary, payload)
        if event_id:
            state.seen_event_ids.add(event_id)
        return True
    except Exception as line_exc:
        # Single bad structure must not kill the whole file drain.
        state.error_count += 1
        now_ts = time.time()
        if now_ts - state.last_error_at >= MONITOR_ERROR_MIN_INTERVAL_S:
            state.last_error_at = now_ts
            add_event(
                agent_id,
                turn_id,
                "observer_monitor_error",
                f"session log line error: {line_exc}"[:2000],
                {"error": repr(line_exc), "source": path.name, "error_count": state.error_count},
            )
        return False


def drain_session_logs(
    agent_id: str,
    turn_id: int,
    cwd: Path,
    session_id: str,
    state: SessionMonitorState,
    *,
    final: bool = False,
) -> int:
    """One pass over session logs. Returns number of events ingested.

    During live polling, incomplete trailing lines (no newline yet) are held.
    On final=True (post-stop drain), the trailing fragment is force-consumed so a
    late tool_completed / test output written without a trailing newline is not lost.
    """
    folder = grok_session_dir(cwd, session_id)
    ingested = 0
    for path in session_log_paths(folder):
        key = str(path)
        try:
            if not path.exists() or not path.is_file():
                continue
            st = path.stat()
            size = int(st.st_size)
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
            offset = _resolve_read_offset(state, path, size, mtime_ns)
            if offset >= size:
                state.offsets[key] = size
                continue
            # Binary read so offsets are stable byte positions across encodings.
            with path.open("rb") as handle:
                handle.seek(offset)
                blob = handle.read()
            if not blob:
                state.offsets[key] = size
                continue
            # Keep incomplete trailing line for the next poll; force-consume on final.
            complete = blob
            consume_to = offset + len(blob)
            if not final and not blob.endswith(b"\n") and not blob.endswith(b"\r"):
                nl = blob.rfind(b"\n")
                if nl < 0:
                    # Entire remainder is incomplete — wait for more bytes or final drain.
                    state.offsets[key] = offset
                    continue
                complete = blob[: nl + 1]
                consume_to = offset + nl + 1

            text_blob = complete.decode("utf-8", errors="replace")
            for line in text_blob.splitlines():
                if _ingest_session_line(agent_id, turn_id, path, line, state):
                    ingested += 1
            state.offsets[key] = min(consume_to, size)
            # Refresh head fingerprint after a successful consume from offset 0 or growth.
            if size > 0:
                fp = _file_head_fingerprint(path, size)
                if fp:
                    state.head_fps[key] = fp
        except OSError:
            continue
    return ingested


def monitor_session(
    agent_id: str,
    turn_id: int,
    cwd: Path,
    session_id: str,
    stopped: threading.Event,
    state: SessionMonitorState | None = None,
    error_box: list | None = None,
) -> SessionMonitorState:
    """Poll session logs until stopped, then perform a deterministic final drain."""
    mon = state or SessionMonitorState()
    try:
        while not stopped.is_set():
            try:
                drain_session_logs(agent_id, turn_id, cwd, session_id, mon, final=False)
            except Exception as exc:
                mon.error_count += 1
                mon.fatal = repr(exc)
                now_ts = time.time()
                if now_ts - mon.last_error_at >= MONITOR_ERROR_MIN_INTERVAL_S:
                    mon.last_error_at = now_ts
                    try:
                        add_event(
                            agent_id,
                            turn_id,
                            "observer_monitor_error",
                            f"monitor poll error: {exc}"[:2000],
                            {"error": repr(exc), "error_count": mon.error_count},
                        )
                    except Exception:
                        pass
                if error_box is not None:
                    error_box.append(repr(exc))
            stopped.wait(0.35)

        # Final drain: wait for session writer flush, then two deterministic passes
        # that force-consume a trailing line without newline.
        time.sleep(SESSION_FLUSH_WAIT_S)
        try:
            drain_session_logs(agent_id, turn_id, cwd, session_id, mon, final=True)
            time.sleep(SESSION_FINAL_DRAIN_EXTRA_S)
            drain_session_logs(agent_id, turn_id, cwd, session_id, mon, final=True)
        except Exception as exc:
            mon.error_count += 1
            mon.fatal = mon.fatal or repr(exc)
            if error_box is not None:
                error_box.append(repr(exc))
            try:
                add_event(
                    agent_id,
                    turn_id,
                    "observer_monitor_error",
                    f"final drain error: {exc}"[:2000],
                    {"error": repr(exc), "phase": "final_drain"},
                )
            except Exception:
                pass
    except Exception as exc:
        mon.fatal = repr(exc)
        if error_box is not None:
            error_box.append(repr(exc))
        try:
            add_event(
                agent_id,
                turn_id,
                "observer_monitor_error",
                f"monitor thread crashed: {exc}"[:2000],
                {"error": repr(exc), "phase": "thread"},
            )
        except Exception:
            pass
    return mon


RUNNERS: dict[str, "AgentRunner"] = {}
RUNNERS_LOCK = threading.Lock()
CONDITIONS: dict[str, threading.Condition] = {}
CONDITIONS_LOCK = threading.Lock()

# Coordination kernel singletons (thread-scoped peer registry, durable mailbox).
REGISTRY = AgentRegistry(coordination_connect)
MAILBOX = Mailbox(coordination_connect, now)
HUB = CoordinationHub(REGISTRY, MAILBOX)

# Streaming / session events that can open or close in-flight tools.
TOOL_ACTIVITY_TYPES = frozenset({
    "tool_call",
    "tool_call_update",
    "tool_started",
    "tool_completed",
    "tool_result",
    "tool_output",
})
TOOL_OPEN_EVENT_TYPES = frozenset({"tool_call", "tool_started"})
TOOL_CLOSE_EVENT_TYPES = frozenset({"tool_completed", "tool_result"})
TOOL_OPEN_STATUSES = frozenset({"", "running", "in_progress", "pending", "started", "accepted"})
TOOL_CLOSED_STATUSES = frozenset({
    "completed", "done", "success", "failed", "error", "cancelled", "canceled", "rejected",
})
UPDATE_MODES = frozenset({"auto", "immediate", "tool_boundary"})


class PendingBoundaryUpdate:
    """One replacement turn waiting for a tool-completion boundary (or timeout)."""

    __slots__ = (
        "turn_id", "turn_no", "prompt", "requested_mode", "timeout_seconds",
        "registered_at", "timer",
    )

    def __init__(
        self,
        turn_id: int,
        turn_no: int,
        prompt: str,
        requested_mode: str,
        timeout_seconds: int,
    ):
        self.turn_id = turn_id
        self.turn_no = turn_no
        self.prompt = prompt
        self.requested_mode = requested_mode
        self.timeout_seconds = timeout_seconds
        self.registered_at = time.time()
        self.timer: threading.Timer | None = None


def _tool_status_from_payload(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    update = _tool_update_root(payload)
    status = str(update.get("status") or payload.get("status") or "").lower().strip()
    outcome = str(update.get("outcome") or payload.get("outcome") or "").lower().strip()
    if outcome in {"success", "ok"}:
        return "completed"
    if outcome in {"failed", "error", "cancelled", "canceled", "rejected"}:
        return outcome
    return status


def _is_tool_closed_status(status: str) -> bool:
    return (status or "").lower().strip() in TOOL_CLOSED_STATUSES


def notify_runner_tool_activity(
    agent_id: str,
    turn_id: int | None,
    event_type: str,
    summary: str,
    payload,
) -> None:
    """Push tool lifecycle signals into the in-memory runner (no DB polling)."""
    if turn_id is None or event_type not in TOOL_ACTIVITY_TYPES:
        return
    with RUNNERS_LOCK:
        runner = RUNNERS.get(agent_id)
    if runner is not None:
        runner.observe_tool_event(int(turn_id), str(event_type), summary or "", payload)


def pending_turn_count(agent_id: str) -> int:
    with connect() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM turns WHERE agent_id=? AND status IN ('queued','running')",
            (agent_id,),
        ).fetchone()
        return int(row["c"])


def unique_changed_files(agent_id: str) -> int:
    with connect() as db:
        row = db.execute("SELECT COUNT(DISTINCT path) AS c FROM changes WHERE agent_id=?", (agent_id,)).fetchone()
        return int(row["c"])


class AgentRunner:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.queue: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.cancelled = threading.Event()
        self._shutdown = threading.Event()
        self._proc_lock = threading.Lock()
        # Turn ids stopped by update_agent. This is distinct from permanent cancel:
        # the worker remains usable and immediately continues with the replacement turn.
        self._interrupted_turns: set[int] = set()
        # In-memory tool-flight + boundary-wait state (never polled from SQLite).
        self._flight_lock = threading.Lock()
        self._active_turn_id: int | None = None
        self._open_tools: dict[str, dict] = {}  # call_id -> {name, title, summary}
        self._pending_boundary: list[PendingBoundaryUpdate] = []
        self.thread = threading.Thread(target=self._work, name=f"grok-{agent_id[:8]}", daemon=True)
        self.thread.start()

    def enqueue(self, turn_id: int, prompt: str) -> None:
        if self.cancelled.is_set() or self._shutdown.is_set():
            raise ValueError("agent is cancelled")
        self.queue.put((turn_id, prompt))

    def has_active_process(self) -> bool:
        with self._proc_lock:
            return self.process is not None and self.process.poll() is None

    def queue_nonempty(self) -> bool:
        return not self.queue.empty()

    def has_inflight_tools(self) -> bool:
        with self._flight_lock:
            return bool(self._open_tools)

    def inflight_tool_snapshot(self) -> list[dict]:
        with self._flight_lock:
            return [dict(v, call_id=k) for k, v in self._open_tools.items()]

    def begin_turn(self, turn_id: int) -> None:
        """Reset tool-flight tracking for a newly started turn."""
        with self._flight_lock:
            self._active_turn_id = turn_id
            self._open_tools.clear()

    def end_turn(self, turn_id: int) -> None:
        """Drop flight state when a turn finishes; release boundary waiters without re-interrupt."""
        with self._flight_lock:
            if self._active_turn_id != turn_id:
                return
            pendings = list(self._pending_boundary)
            self._pending_boundary.clear()
            self._active_turn_id = None
            self._open_tools.clear()
        for pending in pendings:
            self._cancel_pending_timer(pending)
            # Replacement turns are already queued; surface that the wait is over.
            add_event(
                self.agent_id,
                turn_id,
                "update_applied",
                f"回合结束，按更新继续 (turn {pending.turn_no})",
                {
                    "mode_used": "queued_after_completion",
                    "requested_mode": pending.requested_mode,
                    "trigger": "turn_end",
                    "replacement_turn": pending.turn_no,
                    "replacement_turn_id": pending.turn_id,
                    "lossless_interject": False,
                },
            )

    def observe_tool_event(self, turn_id: int, event_type: str, summary: str, payload) -> None:
        """Track open tools from stdout/session events; fire boundary when the last tool closes."""
        if event_type not in TOOL_ACTIVITY_TYPES:
            return
        name, title, _kind, call_id = _tool_identity(payload if isinstance(payload, dict) else {})
        status = _tool_status_from_payload(payload)
        if not call_id:
            # Anonymous lifecycle: open on start events, close on completed/result.
            call_id = f"anon:{(name or title or summary or event_type)[:80]}"
        should_fire = False
        trigger_tool: dict | None = None
        with self._flight_lock:
            if self._active_turn_id != turn_id:
                return
            opened_before = bool(self._open_tools)
            if event_type in TOOL_OPEN_EVENT_TYPES or (
                event_type == "tool_call_update" and not _is_tool_closed_status(status)
            ):
                if event_type != "tool_output":
                    self._open_tools[call_id] = {
                        "name": name or "",
                        "title": title or summary or "",
                        "summary": (summary or "")[:200],
                        "event_type": event_type,
                    }
            if event_type in TOOL_CLOSE_EVENT_TYPES or (
                event_type == "tool_call_update" and _is_tool_closed_status(status)
            ):
                closed = self._open_tools.pop(call_id, None)
                # If call_id mismatched, still clear a single open tool with same name.
                if closed is None and len(self._open_tools) == 1 and name:
                    for key, meta in list(self._open_tools.items()):
                        if meta.get("name") == name:
                            closed = self._open_tools.pop(key, None)
                            call_id = key
                            break
                if closed is not None:
                    trigger_tool = dict(closed, call_id=call_id)
            # Boundary: last in-flight tool closed while updates wait.
            if opened_before and not self._open_tools and self._pending_boundary:
                should_fire = True
                if trigger_tool is None:
                    trigger_tool = {"name": name, "title": title, "call_id": call_id, "event_type": event_type}
        if should_fire:
            self._fire_boundary_updates(reason="tool_boundary", trigger_tool=trigger_tool)

    def register_boundary_wait(self, pending: PendingBoundaryUpdate) -> str:
        """
        Wait for tool completion before interrupting.
        Returns 'waiting_tool_boundary', or 'immediate' if no tools remain (race).
        """
        with self._flight_lock:
            if self.cancelled.is_set() or self._shutdown.is_set():
                return "immediate"
            if not self._open_tools or self._active_turn_id is None:
                return "immediate"
            self._pending_boundary.append(pending)

            def _on_timeout(turn_id: int = pending.turn_id) -> None:
                # Only fire if this pending entry is still waiting.
                self._fire_boundary_updates(reason="timeout", for_turn_id=turn_id)

            timer = threading.Timer(max(1, int(pending.timeout_seconds)), _on_timeout)
            timer.daemon = True
            pending.timer = timer
            timer.start()
            return "waiting_tool_boundary"

    def force_apply_pending_as_immediate(self) -> list[PendingBoundaryUpdate]:
        """Cancel boundary waits (e.g. a later immediate update supersedes them)."""
        with self._flight_lock:
            pendings = list(self._pending_boundary)
            self._pending_boundary.clear()
        for pending in pendings:
            self._cancel_pending_timer(pending)
        return pendings

    def release_all_boundary_waits(self) -> None:
        """Cancel timers on cancel/shutdown so threads never outlive the runner."""
        with self._flight_lock:
            pendings = list(self._pending_boundary)
            self._pending_boundary.clear()
            self._open_tools.clear()
            self._active_turn_id = None
        for pending in pendings:
            self._cancel_pending_timer(pending)

    @staticmethod
    def _cancel_pending_timer(pending: PendingBoundaryUpdate) -> None:
        timer = pending.timer
        pending.timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _fire_boundary_updates(
        self,
        *,
        reason: str,
        trigger_tool: dict | None = None,
        for_turn_id: int | None = None,
    ) -> None:
        """
        Interrupt the active turn once and mark all waiting replacements applied.
        reason: tool_boundary | timeout
        """
        with self._flight_lock:
            if self.cancelled.is_set() or self._shutdown.is_set():
                pendings = list(self._pending_boundary)
                self._pending_boundary.clear()
                active = None
            else:
                if for_turn_id is not None:
                    # Timeout for one entry: only fire if that entry still pending.
                    still = [p for p in self._pending_boundary if p.turn_id == for_turn_id]
                    if not still:
                        return
                pendings = list(self._pending_boundary)
                if not pendings:
                    return
                self._pending_boundary.clear()
                active = self._active_turn_id
        for pending in pendings:
            self._cancel_pending_timer(pending)
        if active is None or self.cancelled.is_set() or self._shutdown.is_set():
            return
        mode_used = "immediate_timeout" if reason == "timeout" else "interrupt_and_resume"
        for pending in pendings:
            add_event(
                self.agent_id,
                active,
                "interjection",
                pending.prompt,
                {
                    "mode": mode_used,
                    "requested_mode": pending.requested_mode,
                    "replacement_turn": pending.turn_no,
                    "trigger": reason,
                    "lossless_interject": False,
                },
            )
            add_event(
                self.agent_id,
                active,
                "update_applied",
                (
                    f"超时后立即中断并更新 (turn {pending.turn_no})"
                    if reason == "timeout"
                    else f"工具边界后中断并更新 (turn {pending.turn_no})"
                ),
                {
                    "mode_used": mode_used,
                    "requested_mode": pending.requested_mode,
                    "trigger": reason,
                    "tool": trigger_tool,
                    "replacement_turn": pending.turn_no,
                    "replacement_turn_id": pending.turn_id,
                    "lossless_interject": False,
                },
            )
        # Single interrupt for the whole batch — queued replacements run in order.
        self.interrupt_turn(active)

    def _terminate_process(self) -> None:
        with self._proc_lock:
            proc = self.process
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except OSError:
            pass

    def interrupt_turn(self, turn_id: int) -> bool:
        """Stop one active turn without cancelling the reusable agent conversation."""
        with self._proc_lock:
            proc = self.process
            self._interrupted_turns.add(turn_id)
            active = bool(proc and proc.poll() is None)
        if active:
            self._terminate_process()
        return True

    def _drain_queue(self) -> list[int]:
        drained: list[int] = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                # Preserve shutdown sentinel for the worker.
                self.queue.put(None)
                break
            drained.append(item[0])
        return drained

    def cancel(self) -> None:
        """Persist cancel: flag, kill process, clear queue, mark all active turns cancelled."""
        self.cancelled.set()
        self.release_all_boundary_waits()
        drained = self._drain_queue()
        self._terminate_process()
        stamp = now()
        with connect() as db:
            agent = db.execute("SELECT id FROM agents WHERE id=?", (self.agent_id,)).fetchone()
            if not agent:
                return
            # Clear child_pid once the process is gone (cancel path).
            db.execute(
                "UPDATE agents SET status='cancelled',child_pid=NULL,updated_at=?,revision=revision+1 WHERE id=?",
                (stamp, self.agent_id),
            )
            db.execute(
                "UPDATE turns SET status='cancelled',completed_at=COALESCE(completed_at, ?) "
                "WHERE agent_id=? AND status IN ('queued','running')",
                (stamp, self.agent_id),
            )
            for turn_id in drained:
                db.execute(
                    "UPDATE turns SET status='cancelled',completed_at=COALESCE(completed_at, ?) WHERE id=? AND status!='cancelled'",
                    (stamp, turn_id),
                )
        add_event(self.agent_id, None, "cancelled", "Codex 已取消此代理")
        notify_agent(self.agent_id)
        # Stop the worker so it does not spin on queue.get for the daemon's life.
        # A cancelled agent rejects further send/update, so the thread is not needed.
        self._shutdown.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass

    def shutdown(self) -> None:
        """Idempotent: stop worker so it does not block forever on queue.get."""
        if self._shutdown.is_set():
            self.thread.join(timeout=2)
            return
        self._shutdown.set()
        self.cancelled.set()
        self.release_all_boundary_waits()
        self._drain_queue()
        self._terminate_process()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=5)

    def _mark_turn_cancelled(self, turn_id: int) -> None:
        stamp = now()
        with connect() as db:
            db.execute(
                "UPDATE turns SET status='cancelled',completed_at=COALESCE(completed_at, ?) WHERE id=?",
                (stamp, turn_id),
            )
            db.execute(
                "UPDATE agents SET status='cancelled',updated_at=?,revision=revision+1 WHERE id=?",
                (stamp, self.agent_id),
            )
        notify_agent(self.agent_id)

    def _work(self) -> None:
        while not self._shutdown.is_set():
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            turn_id, prompt = item
            if self.cancelled.is_set() or self._shutdown.is_set():
                self._mark_turn_cancelled(turn_id)
                continue
            self._run(turn_id, prompt)
        # Drain remaining items as cancelled when shutting down.
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                break
            self._mark_turn_cancelled(item[0])

    def _run(self, turn_id: int, prompt: str) -> None:
        # Never resurrect a cancelled agent into running.
        if self.cancelled.is_set():
            self._mark_turn_cancelled(turn_id)
            return
        with connect() as db:
            agent = db.execute("SELECT * FROM agents WHERE id=?", (self.agent_id,)).fetchone()
            turn = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if not agent or not turn:
                return
            if agent["status"] == "cancelled" or turn["status"] == "cancelled":
                self._mark_turn_cancelled(turn_id)
                return
            db.execute(
                "UPDATE agents SET status='running',current_turn=?,updated_at=?,revision=revision+1 WHERE id=?",
                (turn_id, now(), self.agent_id),
            )
            db.execute("UPDATE turns SET status='running',started_at=? WHERE id=?", (now(), turn_id))
        self.begin_turn(turn_id)
        cwd = Path(agent["cwd"])
        # Capture workspace + session-log baselines BEFORE process start to avoid races
        # and to prevent resume turns from replaying historical updates.jsonl.
        before = workspace_snapshot(cwd)
        session_baseline = capture_session_log_baseline(cwd, agent["grok_session_id"])
        monitor_state = SessionMonitorState(session_baseline)
        add_event(self.agent_id, turn_id, "user", prompt, {"prompt": prompt, "turn": turn["turn_no"]})

        first_turn = int(turn["turn_no"]) == 1
        fake_grok = os.environ.get("GROK_OBSERVER_FAKE_GROK")
        executable = [sys.executable, fake_grok] if fake_grok else ["grok"]
        command = executable + ["-p", prompt, "--cwd", str(cwd), "--output-format", "streaming-json", "--always-approve", "--no-subagents", "--max-turns", "50"]
        command += ["--session-id", agent["grok_session_id"]] if first_turn else ["--resume", agent["grok_session_id"]]
        add_event(self.agent_id, turn_id, "process", "启动 Grok Build", {"command": command[:1] + ["<prompt>"] + command[3:]})

        stopped = threading.Event()
        monitor_errors: list[str] = []
        monitor = threading.Thread(
            target=monitor_session,
            args=(self.agent_id, turn_id, cwd, agent["grok_session_id"], stopped, monitor_state, monitor_errors),
            name=f"mon-{self.agent_id[:8]}-{turn_id}",
            daemon=True,
        )
        monitor.start()
        chunks: list[str] = []
        errors: list[str] = []
        stop_reason = ""
        returncode = 1
        try:
            if self.cancelled.is_set():
                raise RuntimeError("cancelled before start")
            child_env, proxy_source = system_proxy_environment(os.environ.copy())
            child_env.setdefault("PYTHONIOENCODING", "utf-8")
            child_env.update(worker_bridge_env(self.agent_id))
            if proxy_source:
                add_event(self.agent_id, turn_id, "network", f"Grok 已使用代理：{proxy_source}", {"source": proxy_source})
            with self._proc_lock:
                if turn_id in self._interrupted_turns:
                    raise RuntimeError("turn interrupted before Grok process start")
                self.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=NO_WINDOW,
                )
            proc = self.process
            # Persist child PID + its creation time so recover() can reap orphans
            # after a daemon crash without risking a reused-PID mis-kill.
            created = process_create_time(proc.pid)
            with connect() as db:
                db.execute(
                    "UPDATE agents SET child_pid=?,child_started_at=?,updated_at=?,revision=revision+1 WHERE id=?",
                    (proc.pid, str(created) if created is not None else None, now(), self.agent_id),
                )

            def read_stderr():
                assert proc and proc.stderr
                for line in proc.stderr:
                    value = clean_terminal_text(line.rstrip())
                    if value:
                        errors.append(value)
                        add_event(self.agent_id, turn_id, "diagnostic", value[:2000], {"text": value})

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            assert proc.stdout
            pending_type = None
            pending_chunks: list[str] = []

            def flush_chunks():
                nonlocal pending_type, pending_chunks
                if pending_type and pending_chunks:
                    combined = "".join(pending_chunks)
                    add_event(self.agent_id, turn_id, pending_type, combined, {"type": pending_type, "data": combined, "coalesced": True})
                pending_type = None
                pending_chunks = []

            for line in proc.stdout:
                if self.cancelled.is_set():
                    self._terminate_process()
                    break
                value = line.rstrip()
                if not value:
                    continue
                try:
                    event = json.loads(value)
                except json.JSONDecodeError:
                    add_event(self.agent_id, turn_id, "raw", value[:2000], {"text": value})
                    continue
                event_type = event.get("type", "raw")
                data = str(event.get("data") or event.get("message") or "")
                if event_type in {"text", "thought"}:
                    if event_type == "text":
                        chunks.append(data)
                    if pending_type != event_type:
                        flush_chunks()
                        pending_type = event_type
                    pending_chunks.append(data)
                    if sum(map(len, pending_chunks)) >= 4000:
                        flush_chunks()
                    continue
                flush_chunks()
                if event_type == "end":
                    stop_reason = str(event.get("stopReason", ""))
                add_event(self.agent_id, turn_id, event_type, data or event_type, event)
                # streaming-json may also surface tool lifecycle on some builds.
                notify_runner_tool_activity(
                    self.agent_id, turn_id, str(event_type), data or event_type, event
                )
            flush_chunks()
            returncode = proc.wait()
            stderr_thread.join(timeout=2)
        except Exception as exc:
            returncode = 1
            errors.append(str(exc))
            add_event(self.agent_id, turn_id, "error", str(exc), {"exception": repr(exc)})
        finally:
            # End flight tracking before monitor join so late tool closes can still
            # race; release boundary waiters once the process path is done.
            self.end_turn(turn_id)
            # Stop monitor so it runs final drain (flush wait + two deterministic passes).
            stopped.set()
            monitor.join(timeout=5)
            if monitor.is_alive():
                add_event(
                    self.agent_id,
                    turn_id,
                    "observer_monitor_error",
                    "monitor thread did not exit after final drain timeout",
                    {"phase": "join_timeout"},
                )
            if monitor_errors or monitor_state.fatal:
                # Surface monitor death to runner diagnostics (not silent).
                detail = monitor_state.fatal or (monitor_errors[-1] if monitor_errors else "unknown")
                errors.append(f"observer_monitor: {detail}")
            with self._proc_lock:
                proc = self.process
                self.process = None
            if proc is not None:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            # Process ended (or never started); drop durable PID marker.
            with connect() as db:
                db.execute(
                    "UPDATE agents SET child_pid=NULL,updated_at=? WHERE id=?",
                    (now(), self.agent_id),
                )

        final_text = "".join(chunks).strip()
        # Fallback when streaming-json never emitted text chunks (session-only / odd formats).
        if not final_text and not self.cancelled.is_set():
            final_text = final_text_from_events(self.agent_id, turn_id)
        error_text = "\n".join(errors)[-12000:]
        with self._proc_lock:
            was_interrupted = turn_id in self._interrupted_turns
            self._interrupted_turns.discard(turn_id)
        turn_status = "completed" if returncode == 0 else "failed"
        if was_interrupted:
            turn_status = "interrupted"
        if self.cancelled.is_set():
            turn_status = "cancelled"

        with connect() as db:
            current_status = db.execute("SELECT status FROM agents WHERE id=?", (self.agent_id,)).fetchone()["status"]
            if current_status == "cancelled" or self.cancelled.is_set():
                turn_status = "cancelled"
            db.execute(
                "UPDATE turns SET status=?,result=?,stop_reason=?,completed_at=? WHERE id=?",
                (turn_status, final_text, stop_reason, now(), turn_id),
            )
            # final_text is always the latest finished turn result.
            if turn_status == "cancelled" or self.cancelled.is_set():
                agent_status = "cancelled"
            else:
                # Separate turn completion from agent aggregate: stay non-terminal if more work remains.
                queued_left = db.execute(
                    "SELECT COUNT(*) AS c FROM turns WHERE agent_id=? AND status='queued'",
                    (self.agent_id,),
                ).fetchone()["c"]
                if queued_left > 0 or not self.queue.empty():
                    agent_status = "queued"
                else:
                    agent_status = turn_status
            db.execute(
                "UPDATE agents SET status=?,final_text=?,error=?,child_pid=NULL,updated_at=?,revision=revision+1 WHERE id=?",
                (agent_status, final_text, error_text, now(), self.agent_id),
            )
            db.execute("INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)", (self.agent_id, "result", final_text + "\n" + error_text))
        if turn_status != "cancelled":
            record_changes(self.agent_id, turn_id, before, workspace_snapshot(cwd, prior=before))
        terminal_summary = {
            "completed": "Grok 已完成",
            "cancelled": "Grok 已取消",
            "interrupted": "当前回合已中断，正在按更新继续",
        }.get(turn_status, "Grok 执行失败")
        add_event(self.agent_id, turn_id, turn_status, terminal_summary, {"returncode": returncode, "stop_reason": stop_reason})
        notify_agent(self.agent_id)
        if agent_status == "completed":
            deliver_pending_messages(self.agent_id)


def get_runner(agent_id: str, *, create: bool = True) -> AgentRunner | None:
    with RUNNERS_LOCK:
        runner = RUNNERS.get(agent_id)
        if runner is None and create:
            runner = AgentRunner(agent_id)
            RUNNERS[agent_id] = runner
        return runner


def worker_bridge_env(agent_id: str) -> dict:
    """Return the worker-bridge env vars for a spawned agent, backfilling a hub token."""
    with connect() as db:
        row = db.execute("SELECT hub_token FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise ValueError("worker not found")
        token = row["hub_token"]
        if not token:
            token = secrets.token_urlsafe(32)
            db.execute(
                "UPDATE agents SET hub_token=?,updated_at=? WHERE id=?",
                (token, now(), agent_id),
            )
    return {
        "GROK_OBSERVER_AGENT_ID": agent_id,
        "GROK_OBSERVER_AGENT_TOKEN": token,
        "GROK_OBSERVER_CONTROL_PORT": str(ACTUAL_CONTROL_PORT),
    }


def deliver_pending_messages(agent_id: str) -> int:
    """Coalesce pending worker-addressed messages into one follow-up turn.

    Only completed workers receive deliveries; queued, running, failed, and
    cancelled agents are skipped. All DB writes happen in a single transaction
    so the turn insert, agent status update, and message state update commit
    together.
    """
    with connect() as db:
        agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None or agent["status"] in ("queued", "running", "cancelled", "failed"):
            return 0
        messages = db.execute(
            "SELECT id,from_peer,body FROM agent_messages "
            "WHERE to_peer=? AND state='pending' ORDER BY created_at ASC, id ASC",
            (agent_id,),
        ).fetchall()
        if not messages:
            return 0
        n = len(messages)
        header = f"你在 Grok Agent Fabric 中收到 {n} 条协作消息，请统一处理："
        blocks = []
        for i, message in enumerate(messages, start=1):
            body = message["body"] or ""
            if len(body) > 4000:
                body = body[:4000] + "…[截断]"
            blocks.append(f"[{i}] (from {message['from_peer']}, id {message['id']})\n{body}")
        prompt = header + "\n" + "\n---\n".join(blocks)
        if len(prompt.encode("utf-8")) > 60 * 1024:
            prompt = prompt.encode("utf-8")[: 60 * 1024].decode("utf-8", errors="ignore") + "\n[消息过多，已截断]"
        turn_no = db.execute(
            "SELECT COALESCE(MAX(turn_no),0)+1 AS n FROM turns WHERE agent_id=?", (agent_id,)
        ).fetchone()["n"]
        cursor = db.execute(
            "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,?,?,'queued',?)",
            (agent_id, turn_no, prompt, now()),
        )
        turn_id = cursor.lastrowid
        db.execute(
            "UPDATE agents SET status='queued',updated_at=?,revision=revision+1 WHERE id=?",
            (now(), agent_id),
        )
        db.execute(
            "UPDATE agent_messages SET state='delivered',delivered_at=?,target_turn_id=? "
            "WHERE id IN (%s) AND state='pending'" % ",".join("?" * n),
            (now(), turn_id, *[message["id"] for message in messages]),
        )
    with CREATE_LOCK:
        runner = get_runner(agent_id)
        if runner is None or runner.queue.qsize() >= MAX_QUEUE_DEPTH:
            with connect() as db:
                db.execute("UPDATE turns SET status='cancelled',completed_at=? WHERE id=?", (now(), turn_id))
            return 0
        runner.enqueue(turn_id, prompt)
    notify_agent(agent_id)
    add_event(agent_id, turn_id, "hub_delivery", f"投递 {n} 条协作消息", {"messages": n, "turn_id": turn_id, "prompt_preview": prompt[:200]})
    return n


def reclaim_agent_resources(agent_id: str) -> None:
    """Stop runner thread, drop condition, used by delete and cleanup_old."""
    with RUNNERS_LOCK:
        runner = RUNNERS.pop(agent_id, None)
    if runner is not None:
        runner.shutdown()
    with CONDITIONS_LOCK:
        CONDITIONS.pop(agent_id, None)


def rowdict(row) -> dict | None:
    return dict(row) if row else None


def agent_wait_done(agent_id: str) -> tuple[bool, dict]:
    """done only when no process, no active turns, queue empty, and agent terminal."""
    with connect() as db:
        agent = rowdict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
        if not agent:
            raise ValueError("agent not found")
        pending = db.execute(
            "SELECT COUNT(*) AS c FROM turns WHERE agent_id=? AND status IN ('queued','running')",
            (agent_id,),
        ).fetchone()["c"]
        turns = db.execute("SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (agent_id,)).fetchone()["c"]
    with RUNNERS_LOCK:
        runner = RUNNERS.get(agent_id)
    active_process = bool(runner and runner.has_active_process())
    queue_busy = bool(runner and runner.queue_nonempty())
    status = agent["status"]
    done = status in TERMINAL and int(pending) == 0 and not active_process and not queue_busy
    return done, {
        "id": agent["id"],
        "name": agent["name"],
        "status": status,
        "revision": agent["revision"],
        "updated_at": agent["updated_at"],
        "signoff_verdict": agent["signoff_verdict"],
        "final_text": agent["final_text"],
        "error": agent["error"],
        "signoff_summary": agent["signoff_summary"],
        "verification": agent["verification"],
        "turns": turns,
        "pending_turns": int(pending),
        "changed_files": unique_changed_files(agent_id),
    }


def final_text_from_events(agent_id: str, turn_id: int, *, limit: int = 40, cap: int = 12_000) -> str:
    """Reconstruct final_text from recent text-like events when stdout chunks were empty."""
    with connect() as db:
        rows = db.execute(
            "SELECT type, summary, payload, seq FROM events "
            "WHERE agent_id=? AND turn_id=? AND type IN ('text','agent_message_chunk') "
            "ORDER BY seq DESC LIMIT ?",
            (agent_id, turn_id, limit),
        ).fetchall()
    if not rows:
        return ""
    # Chronological order for concatenation.
    parts: list[str] = []
    for row in reversed(list(rows)):
        piece = ""
        payload_raw = row["payload"]
        if payload_raw:
            try:
                payload = json.loads(payload_raw)
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                data = payload.get("data")
                if data is None:
                    data = payload.get("message")
                if data is not None:
                    piece = str(data)
            elif isinstance(payload, str):
                piece = payload
        if not piece:
            piece = str(row["summary"] or "")
        piece = piece.strip()
        if piece:
            parts.append(piece)
    text = "\n".join(parts).strip()
    if len(text) > cap:
        text = text[-cap:]
    return text


def build_search_snippet(content: str, term: str, radius: int = 40) -> dict:
    """Plain-text snippet plus match offsets for safe frontend highlighting."""
    text = content or ""
    # Collapse whitespace for display.
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return {"text": "", "matches": []}
    lower = text.lower()
    needle = term.strip().lower()
    matches: list[dict[str, int]] = []
    if needle:
        start = 0
        while True:
            idx = lower.find(needle, start)
            if idx < 0:
                break
            matches.append({"start": idx, "end": idx + len(needle)})
            start = idx + max(len(needle), 1)
            if len(matches) >= 8:
                break
    if matches:
        first = matches[0]["start"]
        window_start = max(0, first - radius)
        window_end = min(len(text), matches[0]["end"] + radius)
        snippet = text[window_start:window_end]
        if window_start > 0:
            snippet = "…" + snippet
            offset = window_start - 1
        else:
            offset = 0
        if window_end < len(text):
            snippet = snippet + "…"
        # Remap match offsets into snippet coordinates.
        remapped = []
        for m in matches:
            s = m["start"] - offset
            e = m["end"] - offset
            if e <= 0 or s >= len(snippet):
                continue
            remapped.append({"start": max(0, s), "end": min(len(snippet), e)})
        return {"text": snippet, "matches": remapped}
    snippet = text[: radius * 2]
    if len(text) > len(snippet):
        snippet += "…"
    return {"text": snippet, "matches": []}


def action(name: str, args: dict, context: dict) -> dict:
    if name == "ping":
        return {"status": "ok", "viewer_url": viewer_url()}
    if name == "start_viewer":
        opened = ensure_viewer(args.get("agent_id"))
        return {"viewer_url": viewer_url(), "browser_opened": opened}
    if name == "hub":
        thread_id = str(context.get("codex_thread_id") or "unknown")
        return HUB.handle_main(thread_id=thread_id, args=args)
    if name == "create_agent":
        prompt = str(args.get("prompt", "")).strip()
        agent_name = str(args.get("agent_name", "")).strip()
        if not prompt or not agent_name:
            raise ValueError("agent_name and prompt are required")
        agent_id = str(uuid.uuid4())
        thread_id = context.get("codex_thread_id") or "unknown"
        cwd = str(Path(args.get("cwd") or context.get("cwd") or os.getcwd()).resolve())
        stamp = now()
        with CREATE_LOCK, connect() as db:
            # 1) Per-conversation (thread) cap — primary product limit.
            thread_active = db.execute(
                "SELECT COUNT(*) AS c FROM agents "
                "WHERE thread_id=? AND status IN ('queued','running')",
                (thread_id,),
            ).fetchone()["c"]
            if int(thread_active) >= MAX_ACTIVE_PER_THREAD:
                raise ValueError(
                    f"同一对话活跃代理已达上限 MAX_ACTIVE_PER_THREAD={MAX_ACTIVE_PER_THREAD} "
                    f"(set GROK_OBSERVER_MAX_PER_THREAD to raise)"
                )
            # 2) Global safety net across all conversations.
            active_count = db.execute(
                "SELECT COUNT(*) AS c FROM agents WHERE status IN ('queued','running')"
            ).fetchone()["c"]
            if int(active_count) >= MAX_ACTIVE_AGENTS:
                raise ValueError(
                    f"已达全局并发上限 MAX_ACTIVE_AGENTS={MAX_ACTIVE_AGENTS} "
                    f"(set GROK_OBSERVER_MAX_ACTIVE to raise)"
                )
            # Same cwd is allowed (parallel subagents); used only for soft warning.
            same_cwd = db.execute(
                "SELECT id,name FROM agents WHERE cwd=? AND status IN ('queued','running')",
                (cwd,),
            ).fetchall()
            task_title = str(args.get("codex_thread_title") or "").strip() or thread_id
            display_title = derive_display_title(agent_name, prompt)
            db.execute(
                "INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (thread_id, task_title, cwd, context.get("codex_origin", "Codex"), stamp, stamp),
            )
            # Refresh path/title for known threads; keep user pin/archive flags.
            db.execute(
                "UPDATE tasks SET title=?,cwd=?,updated_at=? WHERE thread_id=?",
                (task_title, cwd, stamp, thread_id),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,display_title,hub_token,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                (agent_id, thread_id, agent_name, cwd, agent_id, display_title, secrets.token_urlsafe(32), stamp, stamp),
            )
            cursor = db.execute("INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'queued',?)", (agent_id, prompt, stamp))
            turn_id = cursor.lastrowid
            db.execute(
                "INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)",
                (agent_id, "metadata", f"{agent_name}\n{display_title}\n{prompt}\n{thread_id}\n{task_title}"),
            )
        with CONDITIONS_LOCK:
            CONDITIONS[agent_id] = threading.Condition()
        rule_sources = [str(path) for path in (Path.home() / ".grok" / "AGENTS.md", Path.home() / ".claude" / "Claude.md") if path.exists()]
        add_event(agent_id, turn_id, "rules", "已加载 Grok 规则来源", {"sources": rule_sources})
        if same_cwd:
            add_event(
                agent_id,
                turn_id,
                "concurrency_warning",
                "同一工作目录已有其他 Grok 代理运行，Changes 页文件变更归因可能交叉、不够精确",
                {
                    "other_agents": [dict(row) for row in same_cwd],
                    "hint": "parallel same-cwd agents share the workspace; diffs are recorded per agent/turn but overlapping edits may be attributed to more than one",
                },
            )
        runner = get_runner(agent_id)
        assert runner is not None
        with CREATE_LOCK:
            if runner.queue.qsize() >= MAX_QUEUE_DEPTH:
                # Roll back the just-inserted agent so a full queue leaves no ghost row/FTS.
                with connect() as db:
                    db.execute("DELETE FROM search_index WHERE agent_id=?", (agent_id,))
                    db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
                reclaim_agent_resources(agent_id)
                raise ValueError(
                    f"队列已满 MAX_QUEUE_DEPTH={MAX_QUEUE_DEPTH} "
                    f"(set GROK_OBSERVER_MAX_QUEUE to raise)"
                )
            runner.enqueue(turn_id, prompt)
        opened = ensure_viewer(agent_id)
        return {"agent_id": agent_id, "status": "queued", "viewer_url": f"{viewer_url()}/#/agents/{agent_id}", "browser_opened": opened}
    if name == "send":
        agent_id, prompt = args.get("agent_id"), str(args.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt is required")
        with connect() as db:
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise ValueError("agent not found")
            if agent["status"] == "cancelled":
                raise ValueError("agent is cancelled; send is rejected (no resume API)")
            if agent["status"] == "failed":
                raise ValueError("agent has failed; create a new agent instead")
            number = db.execute("SELECT COALESCE(MAX(turn_no),0)+1 AS n FROM turns WHERE agent_id=?", (agent_id,)).fetchone()["n"]
            cursor = db.execute("INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,?,?,'queued',?)", (agent_id, number, prompt, now()))
            turn_id = cursor.lastrowid
            # While a turn is running, keep aggregate status as running; otherwise queued.
            if agent["status"] != "running":
                db.execute("UPDATE agents SET status='queued',updated_at=?,revision=revision+1 WHERE id=?", (now(), agent_id))
            else:
                db.execute("UPDATE agents SET updated_at=?,revision=revision+1 WHERE id=?", (now(), agent_id))
        runner = get_runner(agent_id)
        assert runner is not None
        with CREATE_LOCK:
            if runner.queue.qsize() >= MAX_QUEUE_DEPTH:
                with connect() as db:
                    db.execute("UPDATE turns SET status='cancelled',completed_at=? WHERE id=?", (now(), turn_id))
                raise ValueError(
                    f"队列已满 MAX_QUEUE_DEPTH={MAX_QUEUE_DEPTH} "
                    f"(set GROK_OBSERVER_MAX_QUEUE to raise)"
                )
            try:
                runner.enqueue(turn_id, prompt)
            except ValueError:
                with connect() as db:
                    db.execute("UPDATE turns SET status='cancelled',completed_at=? WHERE id=?", (now(), turn_id))
                raise
        notify_agent(agent_id)
        return {"agent_id": agent_id, "turn_id": turn_id, "turn_no": number, "status": "queued"}
    if name == "update_agent":
        agent_id, prompt = args.get("agent_id"), str(args.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt is required")
        requested_mode = str(args.get("mode") or "auto").strip().lower()
        if requested_mode not in UPDATE_MODES:
            raise ValueError("mode must be one of: auto, immediate, tool_boundary")
        try:
            timeout_seconds = int(args.get("timeout_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be an integer") from exc
        timeout_seconds = min(max(timeout_seconds, 1), 300)

        with connect() as db:
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise ValueError("agent not found")
            if agent["status"] == "cancelled":
                raise ValueError("agent is cancelled; update is rejected")
            if agent["status"] == "failed":
                raise ValueError("agent has failed; create a new agent instead")
            number = db.execute(
                "SELECT COALESCE(MAX(turn_no),0)+1 AS n FROM turns WHERE agent_id=?", (agent_id,)
            ).fetchone()["n"]
            cursor = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,?,?,'queued',?)",
                (agent_id, number, prompt, now()),
            )
            turn_id = cursor.lastrowid
            current_turn = int(agent["current_turn"] or 0)
            was_running = agent["status"] == "running" and current_turn > 0
            db.execute(
                "UPDATE agents SET status=?,updated_at=?,revision=revision+1 WHERE id=?",
                ("running" if was_running else "queued", now(), agent_id),
            )
        runner = get_runner(agent_id)
        assert runner is not None
        with CREATE_LOCK:
            if runner.queue.qsize() >= MAX_QUEUE_DEPTH:
                with connect() as db:
                    db.execute("UPDATE turns SET status='cancelled',completed_at=? WHERE id=?", (now(), turn_id))
                raise ValueError(
                    f"队列已满 MAX_QUEUE_DEPTH={MAX_QUEUE_DEPTH} "
                    f"(set GROK_OBSERVER_MAX_QUEUE to raise)"
                )
            # Each update is an independent replacement turn; worker drains them in order.
            runner.enqueue(turn_id, prompt)

        mode = "follow_up"
        if was_running:
            # Resolve auto against in-memory tool flight (no DB polling).
            if requested_mode == "auto":
                resolved = "tool_boundary" if runner.has_inflight_tools() else "immediate"
            else:
                resolved = requested_mode

            if resolved == "tool_boundary" and runner.has_inflight_tools():
                pending = PendingBoundaryUpdate(
                    turn_id=int(turn_id),
                    turn_no=int(number),
                    prompt=prompt,
                    requested_mode=requested_mode,
                    timeout_seconds=timeout_seconds,
                )
                wait_mode = runner.register_boundary_wait(pending)
                if wait_mode == "waiting_tool_boundary":
                    inflight = runner.inflight_tool_snapshot()
                    add_event(
                        agent_id,
                        current_turn,
                        "pending_update",
                        f"等待工具边界后更新 (timeout={timeout_seconds}s)",
                        {
                            "mode": "waiting_tool_boundary",
                            "requested_mode": requested_mode,
                            "timeout_seconds": timeout_seconds,
                            "replacement_turn": number,
                            "replacement_turn_id": turn_id,
                            "inflight_tools": inflight,
                            "lossless_interject": False,
                        },
                    )
                    mode = "waiting_tool_boundary"
                else:
                    # Race: tools closed between check and register → immediate interrupt.
                    resolved = "immediate"

            if mode != "waiting_tool_boundary":
                # Immediate path (explicit, auto-no-tools, or boundary race fallback).
                # A later immediate also cancels any prior boundary waits for this turn.
                prior = runner.force_apply_pending_as_immediate()
                for old in prior:
                    add_event(
                        agent_id,
                        current_turn,
                        "update_applied",
                        f"被立即更新抢占 (turn {old.turn_no})",
                        {
                            "mode_used": "interrupt_and_resume",
                            "requested_mode": old.requested_mode,
                            "trigger": "superseded_by_immediate",
                            "replacement_turn": old.turn_no,
                            "replacement_turn_id": old.turn_id,
                            "lossless_interject": False,
                        },
                    )
                add_event(
                    agent_id,
                    current_turn,
                    "interjection",
                    prompt,
                    {
                        "mode": "interrupt_and_resume",
                        "requested_mode": requested_mode,
                        "replacement_turn": number,
                        "lossless_interject": False,
                    },
                )
                add_event(
                    agent_id,
                    current_turn,
                    "update_applied",
                    f"立即中断并更新 (turn {number})",
                    {
                        "mode_used": "interrupt_and_resume",
                        "requested_mode": requested_mode,
                        "trigger": "immediate",
                        "replacement_turn": number,
                        "replacement_turn_id": turn_id,
                        "lossless_interject": False,
                    },
                )
                if runner.interrupt_turn(current_turn):
                    mode = "interrupt_and_resume"
        notify_agent(agent_id)
        return {
            "agent_id": agent_id,
            "turn_id": turn_id,
            "turn_no": number,
            "status": "queued",
            "requested_mode": requested_mode,
            "mode": mode,
            "timeout_seconds": timeout_seconds,
            "lossless_interject": False,
        }
    if name in {"status", "result"}:
        with connect() as db:
            agent = rowdict(db.execute("SELECT * FROM agents WHERE id=?", (args.get("agent_id"),)).fetchone())
            if not agent:
                raise ValueError("agent not found")
            turns = db.execute("SELECT COUNT(*) AS count FROM turns WHERE agent_id=?", (agent["id"],)).fetchone()["count"]
            turn_rows = []
            last_completed_result = ""
            if name == "result":
                turn_rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT turn_no,prompt,status,result,stop_reason,created_at,started_at,completed_at FROM turns WHERE agent_id=? ORDER BY turn_no",
                        (agent["id"],),
                    )
                ]
                last_done = db.execute(
                    "SELECT result FROM turns WHERE agent_id=? AND status='completed' "
                    "ORDER BY turn_no DESC LIMIT 1",
                    (agent["id"],),
                ).fetchone()
                if last_done and last_done["result"]:
                    last_completed_result = str(last_done["result"])
        base = {key: agent[key] for key in ("id", "name", "status", "revision", "updated_at", "signoff_verdict")}
        base.update({"turns": turns, "changed_files": unique_changed_files(agent["id"])})
        if name == "result":
            # Prefer agent.final_text; fall back to last completed turn.result when empty.
            final_text = (agent["final_text"] or "").strip() or last_completed_result
            base.update({
                "final_text": final_text,
                "error": agent["error"],
                "signoff_summary": agent["signoff_summary"],
                "verification": agent["verification"],
                "turn_results": turn_rows,
            })
        return base
    if name == "wait":
        agent_id = args.get("agent_id")
        timeout = min(max(int(args.get("timeout_seconds", 300)), 1), 300)
        with CONDITIONS_LOCK:
            condition = CONDITIONS.setdefault(agent_id, threading.Condition())
        deadline = time.time() + timeout
        while True:
            done, data = agent_wait_done(agent_id)
            if done:
                return {
                    "agent_id": agent_id,
                    "status": data["status"],
                    "done": True,
                    "revision": data["revision"],
                    "turns": data["turns"],
                }
            remaining = deadline - time.time()
            if remaining <= 0:
                return {
                    "agent_id": agent_id,
                    "status": data["status"],
                    "done": False,
                    "timed_out": True,
                    "turns": data["turns"],
                    "pending_turns": data["pending_turns"],
                }
            with condition:
                condition.wait(min(remaining, 0.5))
    if name == "cancel":
        agent_id = args.get("agent_id")
        if not agent_id:
            raise ValueError("agent_id is required")
        with connect() as db:
            agent = db.execute("SELECT id,status FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise ValueError("agent not found")
        runner = get_runner(agent_id, create=False)
        if runner is not None:
            runner.cancel()
        else:
            stamp = now()
            with connect() as db:
                db.execute(
                    "UPDATE agents SET status='cancelled',updated_at=?,revision=revision+1 WHERE id=?",
                    (stamp, agent_id),
                )
                db.execute(
                    "UPDATE turns SET status='cancelled',completed_at=COALESCE(completed_at, ?) "
                    "WHERE agent_id=? AND status IN ('queued','running')",
                    (stamp, agent_id),
                )
            add_event(agent_id, None, "cancelled", "Codex 已取消此代理")
        return {"agent_id": agent_id, "status": "cancelled"}
    if name == "signoff":
        verdict = args.get("verdict")
        if verdict not in {"accepted", "partial", "rejected"}:
            raise ValueError("invalid verdict")
        with connect() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (args.get("agent_id"),)).fetchone():
                raise ValueError("agent not found")
            db.execute("UPDATE agents SET signoff_verdict=?,signoff_summary=?,verification=?,updated_at=?,revision=revision+1 WHERE id=?", (verdict, args.get("summary", ""), args.get("verification", ""), now(), args.get("agent_id")))
        add_event(args["agent_id"], None, "signoff", f"Codex 签收：{verdict}", args)
        return {"agent_id": args["agent_id"], "verdict": verdict, "recorded": True}
    if name == "worker_hub":
        worker_id = str(args.get("worker_id") or "")
        token = str(args.get("worker_token") or "")
        with connect() as db:
            row = db.execute("SELECT hub_token FROM agents WHERE id=?", (worker_id,)).fetchone()
        if row is None or not row["hub_token"] or not secrets.compare_digest(str(row["hub_token"]), token):
            raise ValueError("worker authentication failed")
        return HUB.handle_worker(
            worker_id=worker_id,
            args={k: v for k, v in args.items() if k not in ("worker_id", "worker_token")},
        )
    raise ValueError(f"unknown action: {name}")


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline().decode("utf-8"))
            data = action(request.get("action", ""), request.get("args") or {}, request.get("context") or {})
            response = {"ok": True, "data": data}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


VIEWER_SERVER: ThreadingHTTPServer | None = None
VIEWER_THREAD: threading.Thread | None = None
VIEWER_LOCK = threading.Lock()
ACTUAL_VIEWER_PORT = VIEWER_PORT


def json_response(handler: BaseHTTPRequestHandler, value, status=200):
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "GrokObserver/2.0"

    def log_message(self, *_):
        return

    def _host_is_local(self) -> bool:
        """Reject cross-origin/DNS-rebinding: the Host header must name loopback.

        The socket is already bound to 127.0.0.1, but a hostile page can point a
        DNS name at 127.0.0.1 and read local agent data / artifacts through the
        victim's browser. A rebound request carries the attacker's hostname in
        Host; only genuine localhost clients send a loopback Host.
        """
        raw = (self.headers.get("Host", "") or "").strip()
        if not raw:
            # Missing Host is HTTP/1.0 or a direct socket client, never a browser
            # cross-origin fetch (those always send Host). Treat as local.
            return True
        if raw.startswith("["):
            # IPv6 literal: [::1] or [::1]:port
            host = raw[1:].split("]", 1)[0]
        elif raw.count(":") == 1:
            # host:port (IPv4 / name); bare "::1" has >1 colon and no port
            host = raw.rsplit(":", 1)[0]
        else:
            host = raw
        return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}

    def do_GET(self):
        if not self._host_is_local():
            return json_response(self, {"error": "forbidden"}, 403)
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/bootstrap":
            with connect() as db:
                tasks = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM tasks ORDER BY pinned DESC, archived ASC, updated_at DESC"
                    )
                ]
                agents = [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,thread_id,name,cwd,status,revision,signoff_verdict,"
                        "display_title,pinned,archived,created_at,updated_at "
                        "FROM agents ORDER BY pinned DESC, archived ASC, updated_at DESC"
                    )
                ]
            return json_response(
                self,
                {
                    "tasks": tasks,
                    "agents": agents,
                    "retention_days": RETENTION_DAYS,
                    "catalog_revision": CATALOG_REVISION,
                },
            )
        if parsed.path.startswith("/api/agents/"):
            agent_id = parsed.path.rsplit("/", 1)[-1]
            # Ignore sub-routes that belong to POST (meta/delete).
            if agent_id in {"meta", "delete"} or not re.fullmatch(r"[0-9a-fA-F-]{36}", agent_id or ""):
                return json_response(self, {"error": "not found"}, 404)
            with connect() as db:
                agent = rowdict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
                if not agent:
                    return json_response(self, {"error": "not found"}, 404)
                turns = [dict(row) for row in db.execute("SELECT * FROM turns WHERE agent_id=? ORDER BY turn_no", (agent_id,))]
                changes = [dict(row) for row in db.execute("SELECT * FROM changes WHERE agent_id=? ORDER BY id", (agent_id,))]
            return json_response(self, {"agent": agent, "turns": turns, "changes": changes})
        if parsed.path == "/api/events":
            agent_id = query.get("agent_id", [""])[0]
            after = _safe_int(query.get("after", ["0"])[0])
            with connect() as db:
                events = [dict(row) for row in db.execute("SELECT * FROM events WHERE agent_id=? AND seq>? ORDER BY seq LIMIT 1000", (agent_id, after))]
            return json_response(self, {"events": events})
        if parsed.path == "/api/search":
            term = query.get("q", [""])[0].strip()
            if not term:
                return json_response(self, {"results": []})
            # Pass the term straight to FTS5 MATCH; on a syntax error (special
            # tokens) fall back to a LIKE scan below. No manual escaping is done.
            fts_term = term
            with connect() as db:
                try:
                    rows = db.execute(
                        "SELECT agent_id, kind, content FROM search_index WHERE search_index MATCH ? LIMIT 100",
                        (fts_term,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    # Fallback: simple LIKE if MATCH fails (e.g. special tokens).
                    like = f"%{term}%"
                    rows = db.execute(
                        "SELECT agent_id, kind, content FROM search_index WHERE content LIKE ? LIMIT 100",
                        (like,),
                    ).fetchall()
            results = []
            like = f"%{term}%" if len(term) >= 2 else None
            # Batch the deep-link lookup: one grouped query for the latest matching
            # event per agent, instead of a per-row LIKE scan (was N+1, up to 100).
            deep_links: dict[str, dict] = {}
            if like is not None:
                agent_ids = list(dict.fromkeys(row["agent_id"] for row in rows if row["agent_id"]))
                if agent_ids:
                    placeholders = ",".join("?" * len(agent_ids))
                    with connect() as db:
                        latest = db.execute(
                            f"SELECT agent_id, MAX(id) AS mid FROM events "
                            f"WHERE agent_id IN ({placeholders}) AND (summary LIKE ? OR payload LIKE ?) "
                            f"GROUP BY agent_id",
                            (*agent_ids, like, like),
                        ).fetchall()
                        mids = [r["mid"] for r in latest if r["mid"] is not None]
                        if mids:
                            id_ph = ",".join("?" * len(mids))
                            for hit in db.execute(
                                f"SELECT id, agent_id, turn_id, seq FROM events WHERE id IN ({id_ph})",
                                mids,
                            ):
                                deep_links[hit["agent_id"]] = {
                                    "event_id": hit["id"],
                                    "turn_id": hit["turn_id"],
                                    "event_seq": hit["seq"],
                                }
            for row in rows:
                snippet = build_search_snippet(row["content"] or "", term)
                item = {
                    "agent_id": row["agent_id"],
                    "kind": row["kind"],
                    "snippet": snippet["text"],
                    "matches": snippet["matches"],
                }
                link = deep_links.get(row["agent_id"])
                if link:
                    item.update(link)
                results.append(item)
            return json_response(self, {"results": results})
        if parsed.path == "/api/artifact":
            rel = safe_artifact_relpath(query.get("path", [""])[0])
            if rel is None:
                return json_response(self, {"error": "not found"}, 404)
            try:
                path = (ROOT / rel).resolve()
            except OSError:
                return json_response(self, {"error": "not found"}, 404)
            if not path_is_within(path, ARTIFACTS) or not path.exists() or not path.is_file():
                return json_response(self, {"error": "not found"}, 404)
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            return json_response(self, {"content": content})
        if parsed.path == "/api/stream":
            return self.stream(query.get("agent_id", [""])[0], _safe_int(query.get("after", ["0"])[0]))
        if parsed.path == "/api/stream/catalog":
            return self.stream_catalog()
        return self.static(parsed.path)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 64_000:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_POST(self):
        if not self._host_is_local():
            return json_response(self, {"error": "forbidden"}, 403)
        origin = self.headers.get("Origin", "")
        if origin and origin not in {viewer_url(), viewer_url().replace("127.0.0.1", "localhost")}:
            return json_response(self, {"error": "invalid origin"}, 403)
        if self.path == "/api/viewer/shutdown":
            json_response(self, {"stopping": True})
            threading.Thread(target=stop_viewer, daemon=True).start()
            return
        match = re.fullmatch(r"/api/agents/([0-9a-fA-F-]{36})/delete", self.path)
        if match:
            agent_id = match.group(1)
            with connect() as db:
                agent = db.execute("SELECT id,thread_id,status FROM agents WHERE id=?", (agent_id,)).fetchone()
                if not agent:
                    return json_response(self, {"error": "not found"}, 404)
                if agent["status"] in {"queued", "running"}:
                    return json_response(self, {"error": "running agents cannot be removed from the observer"}, 409)
                thread_id = agent["thread_id"]
            # Reclaim in-memory resources before deleting durable state.
            reclaim_agent_resources(agent_id)
            with connect() as db:
                db.execute("DELETE FROM search_index WHERE agent_id=?", (agent_id,))
                db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
                db.execute("DELETE FROM tasks WHERE thread_id=? AND NOT EXISTS(SELECT 1 FROM agents WHERE thread_id=?)", (thread_id, thread_id))
            shutil.rmtree(ARTIFACTS / agent_id, ignore_errors=True)
            notify_catalog(agent_id)
            return json_response(self, {"deleted": True, "agent_id": agent_id})
        match = re.fullmatch(r"/api/agents/([0-9a-fA-F-]{36})/meta", self.path)
        if match:
            agent_id = match.group(1)
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return json_response(self, {"error": str(exc)}, 400)
            with connect() as db:
                agent = rowdict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
                if not agent:
                    return json_response(self, {"error": "not found"}, 404)
                pinned = agent.get("pinned") or 0
                archived = agent.get("archived") or 0
                display_title = agent.get("display_title") or agent.get("name") or ""
                if "pinned" in body:
                    pinned = 1 if body.get("pinned") else 0
                if "archived" in body:
                    archived = 1 if body.get("archived") else 0
                if "display_title" in body:
                    title = str(body.get("display_title") or "").strip()
                    if not title:
                        return json_response(self, {"error": "display_title cannot be empty"}, 400)
                    if len(title) > 120:
                        title = title[:120]
                    display_title = title
                stamp = now()
                db.execute(
                    "UPDATE agents SET pinned=?,archived=?,display_title=?,updated_at=?,revision=revision+1 WHERE id=?",
                    (pinned, archived, display_title, stamp, agent_id),
                )
                db.execute("UPDATE tasks SET updated_at=? WHERE thread_id=?", (stamp, agent["thread_id"]))
                updated = rowdict(
                    db.execute(
                        "SELECT id,thread_id,name,cwd,status,revision,signoff_verdict,"
                        "display_title,pinned,archived,created_at,updated_at FROM agents WHERE id=?",
                        (agent_id,),
                    ).fetchone()
                )
            notify_agent(agent_id)
            return json_response(self, {"agent": updated})
        match = re.fullmatch(r"/api/tasks/([^/]+)/meta", self.path)
        if match:
            thread_id = urllib.parse.unquote(match.group(1))
            if not thread_id or thread_id.startswith("_orphan:"):
                return json_response(self, {"error": "virtual sessions cannot be edited"}, 400)
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                return json_response(self, {"error": str(exc)}, 400)
            with connect() as db:
                task = rowdict(db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone())
                if not task:
                    return json_response(self, {"error": "not found"}, 404)
                pinned = task.get("pinned") or 0
                archived = task.get("archived") or 0
                title = task.get("title") or thread_id
                if "pinned" in body:
                    pinned = 1 if body.get("pinned") else 0
                if "archived" in body:
                    archived = 1 if body.get("archived") else 0
                if "title" in body:
                    new_title = str(body.get("title") or "").strip()
                    if not new_title:
                        return json_response(self, {"error": "title cannot be empty"}, 400)
                    if len(new_title) > 120:
                        new_title = new_title[:120]
                    title = new_title
                stamp = now()
                db.execute(
                    "UPDATE tasks SET pinned=?,archived=?,title=?,updated_at=? WHERE thread_id=?",
                    (pinned, archived, title, stamp, thread_id),
                )
                updated = rowdict(db.execute("SELECT * FROM tasks WHERE thread_id=?", (thread_id,)).fetchone())
            notify_catalog(None)
            return json_response(self, {"task": updated})
        return json_response(self, {"error": "not found"}, 404)

    def static(self, path: str):
        """Serve viewer/dist with strict path boundary; SPA fallback only for in-root paths."""
        rel = safe_static_relpath(path)
        if rel is None:
            # Hostile / traversal (incl. double-encoded ..) — never SPA-fallback.
            return json_response(self, {"error": "not found"}, 404)

        if not rel:
            target = (STATIC / "index.html").resolve()
        else:
            candidate = (STATIC / rel).resolve()
            if not path_is_within(candidate, STATIC):
                return json_response(self, {"error": "not found"}, 404)
            if candidate.is_file():
                target = candidate
            else:
                # SPA client routes: only if the path stayed inside STATIC.
                target = (STATIC / "index.html").resolve()
                if not path_is_within(target, STATIC):
                    return json_response(self, {"error": "not found"}, 404)

        if not target.exists() or not target.is_file():
            return json_response(self, {"error": "viewer is not built"}, 503)
        if not path_is_within(target, STATIC):
            return json_response(self, {"error": "not found"}, 404)

        mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, agent_id: str, after: int):
        """Event-driven SSE: wait on agent condition instead of 1Hz SQLite polling."""
        # Windows may raise ConnectionAbortedError when the EventSource closes.
        _sse_disconnect = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # Advise clients to reconnect after ~3s if the stream drops.
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
        except _sse_disconnect:
            return
        if not agent_id:
            try:
                self.wfile.write(b'event: bye\ndata: {"reason":"missing_agent"}\n\n')
                self.wfile.flush()
            except _sse_disconnect:
                pass
            return
        # Ensure a waitable condition even for agents created before this stream.
        with CONDITIONS_LOCK:
            condition = CONDITIONS.setdefault(agent_id, threading.Condition())
        last = after
        deadline = time.time() + SSE_STREAM_MAX_S
        try:
            while time.time() < deadline:
                with connect() as db:
                    rows = db.execute(
                        "SELECT * FROM events WHERE agent_id=? AND seq>? ORDER BY seq LIMIT 200",
                        (agent_id, last),
                    ).fetchall()
                emitted = False
                for row in rows:
                    last = row["seq"]
                    self.wfile.write(
                        ("data: " + json.dumps(dict(row), ensure_ascii=False) + "\n\n").encode("utf-8")
                    )
                    emitted = True
                if emitted:
                    self.wfile.flush()
                    # Drain immediately if more events may already be queued.
                    continue
                # Idle: heartbeat then block until notify_agent or timeout.
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                with condition:
                    condition.wait(min(SSE_WAIT_TIMEOUT_S, remaining))
            # Graceful end so EventSource clients can reconnect cleanly.
            self.wfile.write(b'event: bye\ndata: {"reason":"timeout"}\n\n')
            self.wfile.flush()
        except _sse_disconnect:
            return

    def stream_catalog(self):
        """Lightweight SSE for sidebar/list refresh (no tight bootstrap polling)."""
        _sse_disconnect = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            # Seed client with current revision so it can skip stale work.
            with CATALOG_CONDITION:
                rev = CATALOG_REVISION
            self.wfile.write(
                ("data: " + json.dumps({"revision": rev, "kind": "hello"}) + "\n\n").encode("utf-8")
            )
            self.wfile.flush()
        except _sse_disconnect:
            return
        last_rev = rev
        deadline = time.time() + SSE_STREAM_MAX_S
        try:
            while time.time() < deadline:
                with CATALOG_CONDITION:
                    # Wait until revision advances or heartbeat interval elapses.
                    if CATALOG_REVISION == last_rev:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            break
                        CATALOG_CONDITION.wait(min(SSE_CATALOG_HEARTBEAT_S, remaining))
                    rev = CATALOG_REVISION
                if rev != last_rev:
                    last_rev = rev
                    self.wfile.write(
                        (
                            "data: "
                            + json.dumps({"revision": rev, "kind": "change"})
                            + "\n\n"
                        ).encode("utf-8")
                    )
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            self.wfile.write(b'event: bye\ndata: {"reason":"timeout"}\n\n')
            self.wfile.flush()
        except _sse_disconnect:
            return


def viewer_url() -> str:
    return f"http://127.0.0.1:{ACTUAL_VIEWER_PORT}"


def ensure_viewer(agent_id: str | None = None) -> bool:
    global VIEWER_SERVER, VIEWER_THREAD, ACTUAL_VIEWER_PORT
    with VIEWER_LOCK:
        if VIEWER_SERVER is not None:
            return False
        for port in range(VIEWER_PORT, VIEWER_PORT + 20):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
                ACTUAL_VIEWER_PORT = port
                VIEWER_SERVER = server
                break
            except OSError:
                continue
        else:
            raise RuntimeError("no viewer port available")
        VIEWER_THREAD = threading.Thread(target=VIEWER_SERVER.serve_forever, name="grok-viewer", daemon=True)
        VIEWER_THREAD.start()
        write_state()
    target = viewer_url() + (f"/#/agents/{agent_id}" if agent_id else "")
    opened = False
    if os.environ.get("GROK_OBSERVER_NO_BROWSER") != "1":
        opened = bool(webbrowser.open(target))
    return opened


def stop_viewer() -> None:
    global VIEWER_SERVER, VIEWER_THREAD
    with VIEWER_LOCK:
        server = VIEWER_SERVER
        VIEWER_SERVER = None
        VIEWER_THREAD = None
    if server:
        server.shutdown()
        server.server_close()
        write_state()


def write_state() -> None:
    """Atomic state file write so readers never observe a partial JSON document."""
    payload = {
        "pid": os.getpid(),
        "control_port": ACTUAL_CONTROL_PORT,
        "viewer_port": ACTUAL_VIEWER_PORT if VIEWER_SERVER else None,
        "updated_at": now(),
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: write_state runs from several threads (ensure_viewer /
    # stop_viewer / main); a shared .tmp would let concurrent writers interleave.
    tmp = STATE_PATH.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def process_create_time(pid: int) -> float | None:
    """Best-effort process creation time (epoch seconds), or None if unavailable.

    Used to detect PID reuse: a recorded child_pid whose live creation time no
    longer matches what we stored is a different, unrelated process.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_t = wintypes.FILETIME()
            kernel_t = wintypes.FILETIME()
            user_t = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            )
            if not ok:
                return None
            # FILETIME is 100ns ticks since 1601-01-01; convert to Unix epoch seconds.
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            if ticks == 0:
                return None
            return ticks / 10_000_000 - 11_644_473_600
        finally:
            kernel32.CloseHandle(handle)
    # POSIX: derive from /proc/<pid>/stat starttime relative to boot time.
    try:
        raw = open(f"/proc/{pid}/stat", "rb").read()
        # comm (field 2) may contain spaces/parens; slice past the final ')'.
        after = raw[raw.rindex(b")") + 2:].split(b" ")
        starttime_ticks = int(after[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        btime = 0
        with open("/proc/stat", "rb") as sf:
            for line in sf:
                if line.startswith(b"btime "):
                    btime = int(line.split()[1])
                    break
        if not btime or not clk_tck:
            return None
        return btime + starttime_ticks / clk_tck
    except (OSError, ValueError, IndexError):
        return None


def pid_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_pid(pid: int) -> None:
    """Best-effort terminate/kill of a process by pid. Never targets pid 0 or self."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return
    if not pid_is_alive(pid):
        return
    if os.name == "nt":
        import ctypes

        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        # Brief grace so subsequent alive checks observe death.
        for _ in range(20):
            if not pid_is_alive(pid):
                return
            time.sleep(0.05)
        return
    try:
        os.kill(pid, 15)  # SIGTERM
    except OSError:
        return
    for _ in range(20):
        if not pid_is_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, 9)  # SIGKILL
    except OSError:
        pass


def control_ping(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'{"action":"ping"}\n')
            line = sock.makefile("r", encoding="utf-8").readline()
            if not line:
                return False
            response = json.loads(line)
            return bool(response.get("ok"))
    except Exception:
        return False


def healthy_existing_daemon() -> dict | None:
    state = read_state()
    if not state:
        return None
    pid = state.get("pid")
    port = state.get("control_port")
    if not pid_is_alive(int(pid or 0)):
        return None
    if port is None or not control_ping(int(port)):
        return None
    return state


def acquire_singleton_lock() -> bool:
    """Cross-process exclusive lock via lock file. Returns True if this process owns it."""
    global _LOCK_HANDLE
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    # Record owner pid for diagnostics (lock itself is the authority).
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
    except OSError:
        pass
    _LOCK_HANDLE = handle
    return True


def release_singleton_lock() -> None:
    global _LOCK_HANDLE
    handle = _LOCK_HANDLE
    _LOCK_HANDLE = None
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            handle.close()
        except OSError:
            pass
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def cleanup() -> None:
    """Delete expired terminal agents only; reclaim runners/conditions/threads first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    with connect() as db:
        ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM agents WHERE updated_at<? AND status NOT IN ('running','queued')",
                (cutoff,),
            )
        ]
    for agent_id in ids:
        reclaim_agent_resources(agent_id)
        with connect() as db:
            db.execute("DELETE FROM search_index WHERE agent_id=?", (agent_id,))
            db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        shutil.rmtree(ARTIFACTS / agent_id, ignore_errors=True)
    with connect() as db:
        db.execute("DELETE FROM tasks WHERE thread_id NOT IN (SELECT DISTINCT thread_id FROM agents)")
        if ids:
            try:
                db.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass


def cleanup_loop() -> None:
    while True:
        time.sleep(24 * 60 * 60)
        cleanup()


def recover() -> None:
    """On daemon restart: kill recorded orphan children and fail in-flight agents."""
    with connect() as db:
        stale = db.execute(
            "SELECT id, child_pid, child_started_at FROM agents WHERE status IN ('running','queued')"
        ).fetchall()
    for row in stale:
        agent_id = row["id"]
        child_pid = row["child_pid"]
        reaped = False
        skipped_reuse = False
        if child_pid is not None:
            try:
                pid = int(child_pid)
            except (TypeError, ValueError):
                pid = 0
            if pid > 0 and pid_is_alive(pid):
                # Guard against PID reuse: only kill if the live process's creation
                # time still matches what we recorded for this child (±2s tolerance).
                expected = row["child_started_at"]
                actual = process_create_time(pid)
                if expected is not None and actual is not None:
                    try:
                        mismatch = abs(actual - float(expected)) > 2.0
                    except (TypeError, ValueError):
                        mismatch = False
                    if mismatch:
                        skipped_reuse = True
                if not skipped_reuse:
                    terminate_pid(pid)
                    reaped = True
        error = "Observer daemon restarted before completion"
        if reaped:
            error += f"; orphan process reaped (pid={pid})"
        elif skipped_reuse:
            error += f"; recorded pid={pid} was reused by another process, not killed"
        stamp = now()
        with connect() as db:
            db.execute(
                "UPDATE agents SET status='failed',error=?,child_pid=NULL,updated_at=?,revision=revision+1 WHERE id=?",
                (error, stamp, agent_id),
            )
            db.execute(
                "UPDATE turns SET status='failed',completed_at=COALESCE(completed_at, ?) "
                "WHERE agent_id=? AND status IN ('queued','running')",
                (stamp, agent_id),
            )


ACTUAL_CONTROL_PORT = CONTROL_PORT
_WE_OWN_STATE = False


def main() -> None:
    global ACTUAL_CONTROL_PORT, _WE_OWN_STATE

    # If a healthy daemon already owns this data dir, exit without overwriting state.
    existing = healthy_existing_daemon()
    if existing:
        print(json.dumps({"status": "already_running", "pid": existing.get("pid"), "control_port": existing.get("control_port")}, ensure_ascii=False))
        return

    if not acquire_singleton_lock():
        # Another process holds the lock — wait briefly for it to become healthy, then exit.
        for _ in range(40):
            existing = healthy_existing_daemon()
            if existing:
                print(json.dumps({"status": "already_running", "pid": existing.get("pid"), "control_port": existing.get("control_port")}, ensure_ascii=False))
                return
            time.sleep(0.1)
        print(json.dumps({"status": "lock_held", "error": "another daemon is starting or stuck"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    _WE_OWN_STATE = True
    print(f"observer daemon starting pid={os.getpid()}", file=sys.stderr, flush=True)
    init_db()
    cleanup()
    recover()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    # Prefer the configured control port; only fall back if it is free and we own the lock.
    server = None
    for port in range(CONTROL_PORT, CONTROL_PORT + 20):
        try:
            server = ReusableTCPServer(("127.0.0.1", port), ControlHandler)
            ACTUAL_CONTROL_PORT = port
            break
        except OSError:
            continue
    if server is None:
        release_singleton_lock()
        raise RuntimeError("no control port available")

    write_state()
    try:
        server.serve_forever()
    finally:
        stop_viewer()
        # Only the lock owner may clear durable state/lock.
        if _WE_OWN_STATE:
            try:
                STATE_PATH.unlink(missing_ok=True)
            except OSError:
                pass
            release_singleton_lock()


if __name__ == "__main__":
    main()
