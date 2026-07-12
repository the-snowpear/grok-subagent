"""Persistent Grok process supervisor, event store, and local observer web server."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import queue
import re
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


def path_is_within(path: Path, root: Path) -> bool:
    """True iff resolved path is root or a descendant (pathlib boundary, not startswith)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def fully_unquote(value: str, rounds: int = 5) -> str:
    """Decode %XX sequences until stable (catches double/triple encoding)."""
    cur = value
    for _ in range(rounds):
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
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents(
              id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES tasks(thread_id),
              name TEXT NOT NULL, cwd TEXT NOT NULL, grok_session_id TEXT NOT NULL,
              status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              current_turn INTEGER, final_text TEXT DEFAULT '', error TEXT DEFAULT '',
              signoff_verdict TEXT, signoff_summary TEXT, verification TEXT,
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
            """
        )


def artifact(agent_id: str, label: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
    folder = ARTIFACTS / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{label}-{digest}.txt.gz"
    if not path.exists():
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(content)
    return str(path.relative_to(ROOT))


def notify_agent(agent_id: str) -> None:
    with CONDITIONS_LOCK:
        condition = CONDITIONS.get(agent_id)
    if condition:
        with condition:
            condition.notify_all()


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


def fs_snapshot(cwd: Path) -> dict:
    """Strict per-turn filesystem snapshot: rel path -> size/mtime_ns/sha256."""
    entries: dict[str, dict] = {}
    digests: dict[str, str | None] = {}
    total_bytes = 0
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
                digest = _file_digest(root, rel)
                meta = {
                    "kind": "present",
                    "size": size,
                    "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
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


def workspace_snapshot(cwd: Path) -> dict:
    """Prefer Git incremental snapshot; fall back to non-Git FS snapshot (never init Git)."""
    git = git_snapshot(cwd)
    if git.get("available"):
        git = dict(git)
        git["mode"] = "git"
        return git
    return fs_snapshot(cwd)


def record_changes(agent_id: str, turn_id: int, before: dict, after: dict) -> int:
    """Record only this turn's incremental changes vs the pre-turn snapshot."""
    before_mode = before.get("mode") or ("git" if before.get("available") else None)
    after_mode = after.get("mode") or ("git" if after.get("available") else None)

    # Non-Git FS path: require a valid start snapshot for strict start/end deltas.
    # An unavailable/empty before with mode=fs must not treat the whole workspace as "added".
    if after.get("available") and after.get("mode") == "fs":
        if not (before.get("available") and before.get("mode") == "fs"):
            add_event(
                agent_id,
                turn_id,
                "changes",
                "工作区变更检测不可用",
                {
                    "available": False,
                    "count": 0,
                    "reason": before.get("reason") or "changes_unavailable",
                    "error": before.get("error") or "missing start snapshot for non-git workspace",
                    "mode": "fs",
                },
            )
            return 0
        return _record_fs_changes(agent_id, turn_id, before, after)

    if not before.get("available") and not after.get("available"):
        reason = after.get("reason") or before.get("reason") or "changes_unavailable"
        add_event(
            agent_id,
            turn_id,
            "changes",
            "工作区变更检测不可用",
            {
                "available": False,
                "count": 0,
                "reason": reason,
                "error": after.get("error") or before.get("error"),
                "mode": after_mode or before_mode,
            },
        )
        return 0
    if not after.get("available"):
        add_event(
            agent_id,
            turn_id,
            "changes",
            "工作区变更检测不可用",
            {
                "available": False,
                "count": 0,
                "reason": after.get("reason") or "changes_unavailable",
                "error": after.get("error"),
                "mode": after_mode,
            },
        )
        return 0

    # Git path (existing incremental porcelain logic).
    before_entries: dict[str, dict] = before.get("entries") or parse_porcelain_z(before.get("status", ""))
    after_entries: dict[str, dict] = after.get("entries") or parse_porcelain_z(after.get("status", ""))
    before_digests: dict[str, str | None] = before.get("digests") or {}
    after_digests: dict[str, str | None] = after.get("digests") or {}

    stats: dict[str, tuple[int, int]] = {}
    for line in after.get("numstat", "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            stats[parts[2]] = (int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 0)

    # Build this-turn delta only.
    delta: list[tuple[str, str, int, str | None]] = []
    for path, meta in after_entries.items():
        prior = before_entries.get(path)
        if prior is None:
            delta.append((path, meta["kind"], 0, meta.get("rename_from")))
            continue
        # Preexisting dirty: only record if kind/digest/rename changed this turn.
        changed = (
            prior.get("xy") != meta.get("xy")
            or prior.get("rename_from") != meta.get("rename_from")
            or before_digests.get(path) != after_digests.get(path)
        )
        if changed:
            delta.append((path, meta["kind"], 1, meta.get("rename_from")))

    for path, meta in before_entries.items():
        if path in after_entries:
            continue
        # Dirty before, gone after → cleaned or deleted this turn.
        kind = "deleted"
        delta.append((path, kind, 1, meta.get("rename_from")))

    if not delta:
        add_event(agent_id, turn_id, "changes", "本轮无工作区增量变更", {"count": 0, "mode": "git"})
        return 0

    diff_text = after.get("diff", "")
    diff_ref = artifact(agent_id, "git-diff", diff_text) if diff_text else None
    unique_paths: set[str] = set()
    with connect() as db:
        for path, kind, preexisting, rename_from in delta:
            unique_paths.add(path)
            added, deleted = stats.get(path, (0, 0))
            display_path = f"{rename_from} → {path}" if rename_from and kind == "renamed" else path
            db.execute(
                "INSERT INTO changes(agent_id,turn_id,path,kind,preexisting,added,deleted,diff_artifact,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (agent_id, turn_id, display_path if kind == "renamed" and rename_from else path, kind, preexisting, added, deleted, diff_ref, now()),
            )
            db.execute("INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)", (agent_id, "change", path))
    count = len(unique_paths)
    add_event(agent_id, turn_id, "changes", f"检测到 {count} 个本轮工作区变更", {"count": count, "paths": sorted(unique_paths), "mode": "git"})
    return count


def _record_fs_changes(agent_id: str, turn_id: int, before: dict, after: dict) -> int:
    """Diff two FS snapshots: added / modified / deleted (unchanged omitted)."""
    if not after.get("available"):
        add_event(
            agent_id,
            turn_id,
            "changes",
            "工作区变更检测不可用",
            {
                "available": False,
                "count": 0,
                "reason": after.get("reason") or "changes_unavailable",
                "error": after.get("error"),
                "mode": "fs",
            },
        )
        return 0

    before_entries: dict[str, dict] = before.get("entries") or {}
    after_entries: dict[str, dict] = after.get("entries") or {}
    before_digests: dict[str, str | None] = before.get("digests") or {}
    after_digests: dict[str, str | None] = after.get("digests") or {}

    delta: list[tuple[str, str, int]] = []
    for path, meta in after_entries.items():
        prior = before_entries.get(path)
        if prior is None:
            delta.append((path, "added", 0))
            continue
        same = (
            before_digests.get(path) == after_digests.get(path)
            and prior.get("size") == meta.get("size")
            and prior.get("mtime_ns") == meta.get("mtime_ns")
        )
        # Digest is authoritative; mtime-only noise without digest change is ignored.
        if before_digests.get(path) != after_digests.get(path):
            delta.append((path, "modified", 1 if prior else 0))
        elif not same and before_digests.get(path) is None and after_digests.get(path) is None:
            # Both digests missing — fall back to size.
            if prior.get("size") != meta.get("size"):
                delta.append((path, "modified", 1))

    for path in before_entries:
        if path not in after_entries:
            delta.append((path, "deleted", 1))

    if not delta:
        add_event(agent_id, turn_id, "changes", "本轮无工作区增量变更", {"count": 0, "mode": "fs"})
        return 0

    unique_paths: set[str] = set()
    with connect() as db:
        for path, kind, preexisting in delta:
            unique_paths.add(path)
            db.execute(
                "INSERT INTO changes(agent_id,turn_id,path,kind,preexisting,added,deleted,diff_artifact,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (agent_id, turn_id, path, kind, preexisting, 0, 0, None, now()),
            )
            db.execute("INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)", (agent_id, "change", path))
    count = len(unique_paths)
    add_event(
        agent_id,
        turn_id,
        "changes",
        f"检测到 {count} 个本轮工作区变更",
        {"count": count, "paths": sorted(unique_paths), "mode": "fs"},
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
        drained = self._drain_queue()
        self._terminate_process()
        stamp = now()
        with connect() as db:
            agent = db.execute("SELECT id FROM agents WHERE id=?", (self.agent_id,)).fetchone()
            if not agent:
                return
            db.execute(
                "UPDATE agents SET status='cancelled',updated_at=?,revision=revision+1 WHERE id=?",
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

    def shutdown(self) -> None:
        """Idempotent: stop worker so it does not block forever on queue.get."""
        if self._shutdown.is_set():
            self.thread.join(timeout=2)
            return
        self._shutdown.set()
        self.cancelled.set()
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
            if proxy_source:
                add_event(self.agent_id, turn_id, "network", f"Grok 已使用代理：{proxy_source}", {"source": proxy_source})
            with self._proc_lock:
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
            flush_chunks()
            returncode = proc.wait()
            stderr_thread.join(timeout=2)
        except Exception as exc:
            returncode = 1
            errors.append(str(exc))
            add_event(self.agent_id, turn_id, "error", str(exc), {"exception": repr(exc)})
        finally:
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

        final_text = "".join(chunks).strip()
        error_text = "\n".join(errors)[-12000:]
        turn_status = "completed" if returncode == 0 else "failed"
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
                "UPDATE agents SET status=?,final_text=?,error=?,updated_at=?,revision=revision+1 WHERE id=?",
                (agent_status, final_text, error_text, now(), self.agent_id),
            )
            db.execute("INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)", (self.agent_id, "result", final_text + "\n" + error_text))
        if turn_status != "cancelled":
            record_changes(self.agent_id, turn_id, before, workspace_snapshot(cwd))
        terminal_summary = {"completed": "Grok 已完成", "cancelled": "Grok 已取消"}.get(turn_status, "Grok 执行失败")
        add_event(self.agent_id, turn_id, turn_status, terminal_summary, {"returncode": returncode, "stop_reason": stop_reason})
        notify_agent(self.agent_id)


def get_runner(agent_id: str, *, create: bool = True) -> AgentRunner | None:
    with RUNNERS_LOCK:
        runner = RUNNERS.get(agent_id)
        if runner is None and create:
            runner = AgentRunner(agent_id)
            RUNNERS[agent_id] = runner
        return runner


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
    if name == "create_agent":
        prompt = str(args.get("prompt", "")).strip()
        agent_name = str(args.get("agent_name", "")).strip()
        if not prompt or not agent_name:
            raise ValueError("agent_name and prompt are required")
        agent_id = str(uuid.uuid4())
        thread_id = context.get("codex_thread_id") or "unknown"
        cwd = str(Path(args.get("cwd") or context.get("cwd") or os.getcwd()).resolve())
        stamp = now()
        with connect() as db:
            same_cwd = db.execute("SELECT id,name FROM agents WHERE cwd=? AND status IN ('queued','running')", (cwd,)).fetchall()
            db.execute("INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)", (thread_id, args.get("codex_thread_title") or thread_id, cwd, context.get("codex_origin", "Codex"), stamp, stamp))
            db.execute("UPDATE tasks SET title=?,cwd=?,updated_at=? WHERE thread_id=?", (args.get("codex_thread_title") or thread_id, cwd, stamp, thread_id))
            db.execute("INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)", (agent_id, thread_id, agent_name, cwd, agent_id, stamp, stamp))
            cursor = db.execute("INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'queued',?)", (agent_id, prompt, stamp))
            turn_id = cursor.lastrowid
            db.execute("INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)", (agent_id, "metadata", f"{agent_name}\n{prompt}\n{thread_id}\n{args.get('codex_thread_title','')}"))
        with CONDITIONS_LOCK:
            CONDITIONS[agent_id] = threading.Condition()
        rule_sources = [str(path) for path in (Path.home() / ".grok" / "AGENTS.md", Path.home() / ".claude" / "Claude.md") if path.exists()]
        add_event(agent_id, turn_id, "rules", "已加载 Grok 规则来源", {"sources": rule_sources})
        if same_cwd:
            add_event(agent_id, turn_id, "concurrency_warning", "同一工作目录已有其他 Grok 代理运行，文件变更归因可能不精确", {"other_agents": [dict(row) for row in same_cwd]})
        get_runner(agent_id).enqueue(turn_id, prompt)
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
        try:
            runner.enqueue(turn_id, prompt)
        except ValueError:
            with connect() as db:
                db.execute("UPDATE turns SET status='cancelled',completed_at=? WHERE id=?", (now(), turn_id))
            raise
        notify_agent(agent_id)
        return {"agent_id": agent_id, "turn_id": turn_id, "turn_no": number, "status": "queued"}
    if name in {"status", "result"}:
        with connect() as db:
            agent = rowdict(db.execute("SELECT * FROM agents WHERE id=?", (args.get("agent_id"),)).fetchone())
            if not agent:
                raise ValueError("agent not found")
            turns = db.execute("SELECT COUNT(*) AS count FROM turns WHERE agent_id=?", (agent["id"],)).fetchone()["count"]
            turn_rows = []
            if name == "result":
                turn_rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT turn_no,prompt,status,result,stop_reason,created_at,started_at,completed_at FROM turns WHERE agent_id=? ORDER BY turn_no",
                        (agent["id"],),
                    )
                ]
        base = {key: agent[key] for key in ("id", "name", "status", "revision", "updated_at", "signoff_verdict")}
        base.update({"turns": turns, "changed_files": unique_changed_files(agent["id"])})
        if name == "result":
            # final_text is the latest completed turn text (already stored on agent).
            base.update({
                "final_text": agent["final_text"],
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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/bootstrap":
            with connect() as db:
                tasks = [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY updated_at DESC")]
                agents = [dict(row) for row in db.execute("SELECT id,thread_id,name,cwd,status,revision,signoff_verdict,created_at,updated_at FROM agents ORDER BY updated_at DESC")]
            return json_response(self, {"tasks": tasks, "agents": agents, "retention_days": RETENTION_DAYS})
        if parsed.path.startswith("/api/agents/"):
            agent_id = parsed.path.rsplit("/", 1)[-1]
            with connect() as db:
                agent = rowdict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
                if not agent:
                    return json_response(self, {"error": "not found"}, 404)
                turns = [dict(row) for row in db.execute("SELECT * FROM turns WHERE agent_id=? ORDER BY turn_no", (agent_id,))]
                changes = [dict(row) for row in db.execute("SELECT * FROM changes WHERE agent_id=? ORDER BY id", (agent_id,))]
            return json_response(self, {"agent": agent, "turns": turns, "changes": changes})
        if parsed.path == "/api/events":
            agent_id = query.get("agent_id", [""])[0]
            after = int(query.get("after", ["0"])[0])
            with connect() as db:
                events = [dict(row) for row in db.execute("SELECT * FROM events WHERE agent_id=? AND seq>? ORDER BY seq LIMIT 1000", (agent_id, after))]
            return json_response(self, {"events": events})
        if parsed.path == "/api/search":
            term = query.get("q", [""])[0].strip()
            if not term:
                return json_response(self, {"results": []})
            # Escape FTS5 special characters by quoting the query as a phrase when needed.
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
            for row in rows:
                snippet = build_search_snippet(row["content"] or "", term)
                results.append({
                    "agent_id": row["agent_id"],
                    "kind": row["kind"],
                    "snippet": snippet["text"],
                    "matches": snippet["matches"],
                })
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
            return self.stream(query.get("agent_id", [""])[0], int(query.get("after", ["0"])[0]))
        return self.static(parsed.path)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if origin and origin not in {viewer_url(), viewer_url().replace("127.0.0.1", "localhost")}:
            return json_response(self, {"error": "invalid origin"}, 403)
        if self.path == "/api/viewer/shutdown":
            json_response(self, {"stopping": True})
            threading.Thread(target=stop_viewer, daemon=True).start()
            return
        match = re.fullmatch(r"/api/agents/([0-9a-f-]+)/delete", self.path)
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
            return json_response(self, {"deleted": True, "agent_id": agent_id})
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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = after
        try:
            for _ in range(600):
                with connect() as db:
                    rows = db.execute("SELECT * FROM events WHERE agent_id=? AND seq>? ORDER BY seq LIMIT 200", (agent_id, last)).fetchall()
                for row in rows:
                    last = row["seq"]
                    self.wfile.write(("data: " + json.dumps(dict(row), ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
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
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def read_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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


def cleanup_loop() -> None:
    while True:
        time.sleep(24 * 60 * 60)
        cleanup()


def recover() -> None:
    with connect() as db:
        stale = db.execute("SELECT id FROM agents WHERE status IN ('running','queued')").fetchall()
        for row in stale:
            db.execute(
                "UPDATE agents SET status='failed',error='Observer daemon restarted before completion',updated_at=?,revision=revision+1 WHERE id=?",
                (now(), row["id"]),
            )
            db.execute(
                "UPDATE turns SET status='failed',completed_at=COALESCE(completed_at, ?) "
                "WHERE agent_id=? AND status IN ('queued','running')",
                (now(), row["id"]),
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
