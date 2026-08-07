"""Read Grok session telemetry from an existing persisted session over ACP.

The observer still executes delegated work through Grok headless mode. After a
terminal turn, a short-lived ACP process loads the same session and queries
read-only x.ai extension methods. ContextInfo remains the source of semantic
context breakdown; session usage is optional and falls back to streaming-json
usage when unavailable.
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
    pass


_EOF = object()


def unwrap_extension_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("result")
    return nested if isinstance(nested, dict) else value


unwrap_session_info = unwrap_extension_result


def context_summary(info: dict[str, Any]) -> str:
    context = info.get("context") if isinstance(info.get("context"), dict) else {}
    used = int(context.get("used") or 0)
    total = int(context.get("total") or 0)
    pct = int(context.get("usagePct") or 0)
    return f"Context {used}/{total} ({pct}%)" if total else "Context telemetry"


class _JsonRpcStdio:
    def __init__(self, cwd: Path, env: dict[str, str], timeout: float = 8.0):
        self.timeout = timeout
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                ["grok", "agent", "--always-approve", "stdio"],
                cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=flags,
            )
        except OSError as exc:
            raise AcpProbeError(str(exc)) from exc
        self.messages: queue.Queue[object] = queue.Queue()
        self.request_id = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            assert self.proc.stdout
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(_EOF)

    def request(self, method: str, params: dict[str, Any], timeout: float | None = None):
        self.request_id += 1
        rid = self.request_id
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            try:
                item = self.messages.get(timeout=max(0.1, deadline-time.monotonic()))
            except queue.Empty:
                continue
            if item is _EOF:
                raise AcpProbeError("ACP closed")
            if isinstance(item, dict) and item.get("id") == rid:
                if item.get("error"):
                    raise AcpProbeError(str(item["error"]))
                return item.get("result")
        raise AcpProbeError(f"timeout: {method}")

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def probe_session_telemetry(session_id: str, cwd: str | Path, *, env=None, timeout=8.0):
    workdir = Path(cwd).resolve()
    if not session_id:
        raise AcpProbeError("missing session id")
    with _JsonRpcStdio(workdir, dict(env or os.environ), timeout) as rpc:
        rpc.request("initialize", {"protocolVersion":1,"clientCapabilities":{}})
        rpc.request("session/load", {"sessionId":session_id,"cwd":str(workdir),"mcpServers":[]})
        info = unwrap_extension_result(rpc.request("x.ai/session/info", {"sessionId":session_id}))
        usage = {}
        try:
            usage = unwrap_extension_result(rpc.request("x.ai/session/usage", {"sessionId":session_id}))
        except AcpProbeError:
            pass
        return {"info": info, "usage": usage}


def probe_session_context(session_id: str, cwd: str | Path, *, env=None, timeout=8.0):
    return probe_session_telemetry(session_id, cwd, env=env, timeout=timeout)["info"]
