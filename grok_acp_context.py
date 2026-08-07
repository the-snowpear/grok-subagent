"""Read semantic context telemetry from an existing Grok session over ACP.

The observer executes delegated work through Grok headless mode. After a turn
finishes, this module can start a short-lived ``grok agent stdio`` process,
load the same persisted session, call ``x.ai/session/info``, and exit.

This keeps execution unchanged while exposing Grok's own ContextInfo breakdown:
system prompt, messages, tool definitions, skills/MCP estimates, free tokens,
turn/tool/compaction counters, and the resolved auto-compact threshold.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class AcpProbeError(RuntimeError):
    """Raised when the short-lived ACP probe cannot complete safely."""


_EOF = object()


def unwrap_session_info(value: Any) -> dict[str, Any]:
    """Unwrap Grok's extension-method envelope for ``x.ai/session/info``.

    The JSON-RPC response contributes the outer ``result``. Grok's extension
    method currently returns ``{result: SessionInfoResponse, error?: ...}``
    inside that value. Future builds may return the raw response, so this is
    deliberately liberal.
    """
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
    if total:
        return f"Context {used}/{total} ({pct}%)"
    return "Context telemetry"


class _JsonRpcStdio:
    """Tiny newline-delimited JSON-RPC client with bounded waits.

    ACP sends notifications while ``session/load`` replays the conversation.
    A dedicated reader thread lets requests ignore those notifications without
    ever blocking past their deadline on ``readline()``.
    """

    def __init__(self, cwd: Path, env: dict[str, str], timeout: float = 8.0):
        self.timeout = max(2.0, float(timeout))
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                ["grok", "agent", "--always-approve", "stdio"],
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
                    # ACP stdout should be JSON-RPC only. Ignore a noisy line rather
                    # than turning observability into a failure of the delegated turn.
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
            if not isinstance(item, dict):
                continue
            # session/update and x.ai/* notifications have no matching request id.
            if item.get("id") != request_id:
                continue
            if item.get("error") is not None:
                error = item.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("data") or error
                else:
                    detail = error
                raise AcpProbeError(f"ACP {method} failed: {detail}")
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


def probe_session_context(
    session_id: str,
    cwd: str | Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Return Grok's ``SessionInfoResponse`` for a persisted session.

    The probe is read-oriented: it loads the session into a disposable ACP
    process, asks for session info, and terminates the process. ``mcpServers``
    is empty so the observer does not inject any extra MCP integrations.
    Grok's own configured/managed tools and skill baseline still participate in
    the loaded session's ContextInfo.
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

    started = time.monotonic()
    with _JsonRpcStdio(workdir, child_env, timeout=timeout) as rpc:
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
                    "clientVersion": "2.1.0",
                },
            },
        )
        init = init_raw if isinstance(init_raw, dict) else {}
        method_id = _pick_auth_method(init)
        if method_id:
            rpc.request(
                "authenticate",
                {"methodId": method_id, "_meta": {"headless": True}},
            )

        elapsed = time.monotonic() - started
        rpc.request(
            "session/load",
            {
                "sessionId": sid,
                "cwd": str(workdir),
                "mcpServers": [],
                "_meta": {"yoloMode": True},
            },
            timeout=max(1.5, timeout - elapsed),
        )
        elapsed = time.monotonic() - started
        raw_info = rpc.request(
            "x.ai/session/info",
            {"sessionId": sid},
            timeout=max(1.0, timeout - elapsed),
        )
        info = unwrap_session_info(raw_info)
        context = info.get("context") if isinstance(info.get("context"), dict) else None
        if not context or not int(context.get("total") or 0):
            raise AcpProbeError("x.ai/session/info returned no context snapshot")
        return info
