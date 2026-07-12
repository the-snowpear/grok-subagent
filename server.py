"""Codex MCP stdio bridge for the local Grok Agent Observer daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


# MCP stdio is UTF-8 regardless of the active Windows console code page.
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "data" / "daemon-state.json"
DAEMON = ROOT / "daemon.py"


TOOLS = [
    {
        "name": "create_agent",
        "description": "Create an asynchronous observable Grok subagent and return immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
                "codex_thread_title": {"type": "string"},
            },
            "required": ["agent_name", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send",
        "description": "Queue a follow-up prompt in an existing Grok subagent conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "prompt": {"type": "string"}},
            "required": ["agent_id", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "status",
        "description": "Read status once for manual inspection. Do not poll; use wait for completion.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait",
        "description": "Wait until the Grok subagent completes, fails, or is cancelled. Intermediate events stay in the viewer and never wake this call. Call once, then use result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 300},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "result",
        "description": "Return only the final Grok result and compact change/test metadata for Codex review.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel",
        "description": "Cancel a running Grok subagent. This control is intentionally absent from the viewer.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "signoff",
        "description": "Record Codex review after verification: accepted, partial, or rejected.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["accepted", "partial", "rejected"]},
                "summary": {"type": "string"},
                "verification": {"type": "string"},
            },
            "required": ["agent_id", "verdict", "summary"],
            "additionalProperties": False,
        },
    },
]


def _state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _request(payload: dict, timeout: float = 65) -> dict:
    state = _state()
    if not state:
        raise ConnectionError("observer daemon state is unavailable")
    with socket.create_connection(("127.0.0.1", int(state["control_port"])), timeout=3) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        reader = sock.makefile("r", encoding="utf-8")
        line = reader.readline()
        if not line:
            raise ConnectionError("observer daemon closed the connection")
        response = json.loads(line)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "observer request failed"))
        return response.get("data", {})


def _ensure_daemon() -> None:
    try:
        _request({"action": "ping"}, timeout=3)
        return
    except Exception:
        pass

    ROOT.joinpath("data").mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [sys.executable, str(DAEMON)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.15)
        try:
            _request({"action": "ping"}, timeout=1)
            return
        except Exception:
            continue
    raise RuntimeError("Grok Observer daemon did not start")


def call_tool(name: str, args: dict) -> dict:
    _ensure_daemon()
    payload = {"action": name, "args": args}
    if name == "create_agent":
        payload["context"] = {
            "codex_thread_id": os.environ.get("CODEX_THREAD_ID", "unknown"),
            "codex_origin": os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex"),
            "cwd": os.getcwd(),
        }
    timeout = min(max(int(args.get("timeout_seconds", 300)), 1), 300) + 5 if name == "wait" else 15
    data = _request(payload, timeout=timeout)
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        message_id = message.get("id")
        if message_id is None:
            continue
        method = message.get("method")
        try:
            if method == "initialize":
                value = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "grok-agent-observer", "version": "2.0.0"},
                }
            elif method == "ping":
                value = {}
            elif method == "tools/list":
                value = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                if name not in {tool["name"] for tool in TOOLS}:
                    raise ValueError(f"Unknown tool: {name}")
                value = call_tool(name, params.get("arguments") or {})
            else:
                raise ValueError(f"Method not found: {method}")
            send({"jsonrpc": "2.0", "id": message_id, "result": value})
        except Exception as exc:
            send({"jsonrpc": "2.0", "id": message_id, "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
