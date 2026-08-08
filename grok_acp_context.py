"""Read semantic context telemetry from an existing Grok session over ACP.

Delegated work still runs through Grok headless mode. After an idle terminal
turn, the observer starts a short-lived ``grok agent --always-approve stdio``
process, loads the same persisted session, calls ``x.ai/session/info``, then
exits. This exposes Grok's own ContextInfo breakdown without changing execution.

Important: ``x.ai/session/usage`` is intentionally NOT used for cumulative
usage here. Grok's implementation documents that the in-memory usage ledger
resets when a session is resumed in a new agent process, which is exactly what
this disposable ACP probe does. Cumulative tokens/cost therefore remain sourced
from headless ``streaming-json`` usage/end events.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class AcpProbeError(RuntimeError):
    """Raised when the disposable ACP probe cannot complete safely.

    ``code``/``method``/``data`` carry the structured JSON-RPC error details
    when the failure came from an error response; they are ``None`` otherwise.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        method: str | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.method = method
        self.data = data


_EOF = object()


def unwrap_session_info(value: Any) -> dict[str, Any]:
    """Unwrap the inner Grok extension-method result envelope if present."""
    if not isinstance(value, dict):
        return {}
    if "result" in value:
        nested = value.get("result")
        return nested if isinstance(nested, dict) else {}
    return value


def context_summary(info: dict[str, Any]) -> str:
    context = info.get("context") if isinstance(info.get("context"), dict) else {}
    used = int(context.get("used") or 0)
    total = int(context.get("total") or 0)
    pct = int(context.get("usagePct") or 0)
    return f"Context {used}/{total} ({pct}%)" if total else "Context telemetry"


class _JsonRpcStdio:
    """Small newline-delimited JSON-RPC client with bounded waits."""

    def __init__(
        self,
        cwd: Path,
        env: dict[str, str],
        timeout: float = 8.0,
        executable: str | None = None,
    ):
        self.timeout = max(2.0, float(timeout))
        # The cache identity resolves to this exact path, so the probe must
        # Popen the same executable to keep the cache key and launch target
        # consistent. Falls back to PATH lookup of "grok" for standalone use.
        self.executable = executable or "grok"
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                [self.executable, "agent", "--always-approve", "stdio"],
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise AcpProbeError(f"failed to start grok ACP: {exc}") from exc

        self._messages: queue.Queue[object] = queue.Queue()
        self._request_id = 0
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="grok-acp-context",
            daemon=True,
        )
        self._reader.start()

    def _read_stdout(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._messages.put(json.loads(line))
                except json.JSONDecodeError:
                    # Telemetry is best-effort. Do not fail delegated work because
                    # a future Grok build writes a non-JSON informational line.
                    continue
        finally:
            self._messages.put(_EOF)

    def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if self.proc.poll() is not None:
            raise AcpProbeError(f"grok ACP exited with {self.proc.returncode}")

        self._request_id += 1
        request_id = self._request_id
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AcpProbeError(f"failed to write ACP {method}: {exc}") from exc

        deadline = time.monotonic() + (self.timeout if timeout is None else max(0.2, timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpProbeError(f"ACP {method} timed out")
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise AcpProbeError(f"ACP {method} timed out") from exc
            if item is _EOF:
                raise AcpProbeError(f"grok ACP closed during {method}")
            if not isinstance(item, dict) or item.get("id") != request_id:
                # session/load replays notifications; ignore anything that does
                # not correspond to the current request id.
                continue
            if item.get("error") is not None:
                error = item.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("data") or error
                    code = error.get("code")
                    data = error.get("data")
                else:
                    detail = error
                    code = None
                    data = None
                raise AcpProbeError(
                    f"ACP {method} failed: {detail}",
                    code=code,
                    method=method,
                    data=data,
                )
            return item.get("result")

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.25)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=0.75)
                except subprocess.TimeoutExpired:
                    pass
        self._reader.join(timeout=0.5)

    def __enter__(self) -> "_JsonRpcStdio":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _pick_auth_method(init: dict[str, Any]) -> str | None:
    meta = init.get("_meta") if isinstance(init.get("_meta"), dict) else {}
    method_id = meta.get("defaultAuthMethodId")
    if isinstance(method_id, str) and method_id:
        return method_id
    methods = init.get("authMethods") if isinstance(init.get("authMethods"), list) else []
    for method in methods:
        if isinstance(method, dict) and isinstance(method.get("id"), str) and method["id"]:
            return method["id"]
    return None


def _remaining(deadline: float, floor: float) -> float:
    return max(floor, deadline - time.monotonic())


def probe_session_context(
    session_id: str,
    cwd: str | Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 8.0,
    executable: str | None = None,
) -> dict[str, Any]:
    """Load a persisted Grok session and return its SessionInfoResponse.

    ``executable`` is the resolved grok binary path used by the ACP probe; it
    defaults to PATH lookup of ``grok`` when omitted. Callers that key the
    negative cache by ``grok_binary_identity()`` must pass ``identity[0]`` so
    the launched process matches the cached identity.
    """
    sid = str(session_id or "").strip()
    if not sid:
        raise AcpProbeError("missing Grok session id")

    workdir = Path(cwd).expanduser().resolve()
    if not workdir.exists() or not workdir.is_dir():
        raise AcpProbeError(f"session cwd does not exist: {workdir}")

    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("NO_COLOR", "1")
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    deadline = time.monotonic() + max(3.0, float(timeout))

    with _JsonRpcStdio(workdir, child_env, timeout=timeout, executable=executable) as rpc:
        init_raw = rpc.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "_meta": {
                    "startupHints": {
                        "nonInteractive": True,
                        "skipGitStatus": True,
                        "skipProjectLayout": True,
                    },
                    "clientType": "grok-observer",
                    "clientIdentifier": "grok-observer",
                    "clientVersion": "2.2.0",
                },
            },
            timeout=_remaining(deadline, 1.5),
        )
        init = init_raw if isinstance(init_raw, dict) else {}
        method_id = _pick_auth_method(init)
        if method_id:
            rpc.request(
                "authenticate",
                {"methodId": method_id, "_meta": {"headless": True}},
                timeout=_remaining(deadline, 1.0),
            )

        rpc.request(
            "session/load",
            {
                "sessionId": sid,
                "cwd": str(workdir),
                "mcpServers": [],
                "_meta": {"yoloMode": True},
            },
            timeout=_remaining(deadline, 1.5),
        )

        raw_info = rpc.request(
            "x.ai/session/info",
            {"sessionId": sid},
            timeout=_remaining(deadline, 1.0),
        )
        info = unwrap_session_info(raw_info)
        context = info.get("context") if isinstance(info.get("context"), dict) else None
        if not context or not int(context.get("total") or 0):
            raise AcpProbeError("x.ai/session/info returned no context snapshot")
        return info


_UNSUPPORTED_METHOD = "x.ai/session/info"
_JSONRPC_METHOD_NOT_FOUND = -32601

_VERSION_PATTERN = re.compile(r"(\d+(?:\.\d+)*)")
_MISSING = object()

# Version lookup is memoized per (path, size, mtime_ns). Replacing the binary
# changes the fingerprint, which forces a fresh lookup and a new identity, so a
# cached "unsupported" verdict does not survive a grok upgrade.
_version_memo: dict[tuple[str, int, int], str | None] = {}
_version_memo_lock = threading.Lock()
# Full identity per resolved path, reused while the stat fingerprint is
# unchanged, so steady-state turns only pay a cheap path/stat + dict lookup.
_identity_cache: dict[str, tuple[str, str | None, int, int]] = {}
_identity_cache_lock = threading.Lock()


def _run_grok_version(path: Path) -> str | None:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3.0,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    blob = (completed.stdout or "") + "\n" + (completed.stderr or "")
    match = _VERSION_PATTERN.search(blob)
    return match.group(1) if match else None


def _version_for(path: Path, st: Any) -> str | None:
    memo_key = (str(path), st.st_size, st.st_mtime_ns)
    with _version_memo_lock:
        version = _version_memo.get(memo_key, _MISSING)
    if version is _MISSING:
        version = _run_grok_version(path)
        with _version_memo_lock:
            _version_memo.setdefault(memo_key, version)
    return version


def grok_binary_identity(
    *, resolve_version: bool = True
) -> tuple[str, str | None, int, int] | None:
    """Return a cheap identity for the resolved ``grok`` executable.

    Identity is ``(resolved absolute path, version, size, mtime_ns)``; the
    resolved path is exactly the executable the ACP probe will Popen, keeping
    the cache key and launch target consistent. ``version`` is looked up at
    most once per (path, size, mtime) via a memoized ``grok --version`` call,
    and the full identity is reused while the stat fingerprint is unchanged,
    so later turns cost only a path/stat check.

    Pass ``resolve_version=False`` for a scheduler-side pre-check that never
    spawns a subprocess: it returns the memoized identity only when the
    fingerprint is unchanged, otherwise ``None`` so the probe worker resolves
    the fresh identity itself. Returns ``None`` when ``grok`` cannot be
    resolved.
    """
    exe = shutil.which("grok")
    if not exe:
        return None
    try:
        resolved = Path(exe).resolve()
        st = resolved.stat()
    except OSError:
        return None
    key = str(resolved)
    with _identity_cache_lock:
        cached = _identity_cache.get(key)
    if cached is not None and cached[2] == st.st_size and cached[3] == st.st_mtime_ns:
        return cached
    if not resolve_version:
        return None
    version = _version_for(resolved, st)
    identity = (key, version, st.st_size, st.st_mtime_ns)
    with _identity_cache_lock:
        _identity_cache[key] = identity
    return identity


def probe_is_unsupported(err: Any) -> bool:
    """True only for a definitive JSON-RPC ``-32601`` on ``x.ai/session/info``."""
    return (
        isinstance(err, AcpProbeError)
        and err.code == _JSONRPC_METHOD_NOT_FOUND
        and err.method == _UNSUPPORTED_METHOD
    )


class AcpUnsupportedCache:
    """Process-local negative cache for definitively unsupported ACP methods.

    A thread-safe, in-memory-only set of ``(identity, method)`` keys. Nothing
    is ever written to disk, so a process restart always re-probes. Only a
    real probe that received JSON-RPC ``-32601`` for the method may record an
    entry.
    """

    def __init__(self) -> None:
        self._entries: set[tuple[Any, str]] = set()
        self._lock = threading.Lock()

    def record_unsupported(self, identity: Any, method: str) -> bool:
        """Remember that ``method`` is unsupported for ``identity``.

        Returns True when a new entry was actually added (first time for this
        identity/method), False when it was already recorded.
        """
        key = (identity, method)
        with self._lock:
            if key in self._entries:
                return False
            self._entries.add(key)
            return True

    def is_unsupported(self, identity: Any, method: str) -> bool:
        with self._lock:
            return (identity, method) in self._entries

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


acp_unsupported_cache = AcpUnsupportedCache()
