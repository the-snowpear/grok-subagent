"""Native Grok MCP transport for the observer coordination hub.

Exposes the same worker hub operations as grok_hub.py (peers/send/inbox/wait)
as MCP tools over stdio, so a Grok agent runtime with MCP support can
coordinate natively instead of shelling out to the CLI. The CLI stays as a
fallback for non-MCP runtimes.

Worker identity comes from the environment:

- GROK_OBSERVER_AGENT_ID: the worker's own agent id.
- GROK_OBSERVER_AGENT_TOKEN: the worker's hub token (authenticates worker_hub actions).
- GROK_OBSERVER_CONTROL_PORT: the daemon control port (default 47830).

The daemon injects these variables when it spawns the worker.
"""

from __future__ import annotations

import json
import os
import socket
import sys

DEFAULT_PORT = 47830

SERVER_NAME = "grok-agent-observer-native"
VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"


# MCP stdio is UTF-8 regardless of the active Windows console code page.
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


TOOLS = [
    {
        "name": "peers",
        "description": "List same-thread worker peers plus Main for the current agent.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "send",
        "description": "Send a durable hub message to a same-thread worker or to Main.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "message": {"type": "string"},
                "reply_to": {"type": "string"},
            },
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "inbox",
        "description": "Read this agent's hub inbox; peek=true does not consume.",
        "inputSchema": {
            "type": "object",
            "properties": {"peek": {"type": "boolean", "default": False}},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait",
        "description": "Wait for a message addressed to this agent; returns kind message or timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "default": 120},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
]


def op_args_for(name: str, args: dict) -> dict:
    """Map an MCP tool call to the worker hub op arguments."""
    if name == "peers":
        return {"op": "list"}
    if name == "send":
        op_args = {"op": "send", "to": args.get("to"), "message": args.get("message")}
        if args.get("reply_to") is not None:
            op_args["reply_to"] = args["reply_to"]
        return op_args
    if name == "inbox":
        return {"op": "inbox", "peek": bool(args.get("peek", False))}
    if name == "wait":
        op_args = {"op": "wait", "timeout_seconds": int(args.get("timeout_seconds", 120))}
        if args.get("from"):
            op_args["from"] = args["from"]
        return op_args
    raise ValueError(f"Unknown tool: {name}")


def identity() -> tuple[str, str, int]:
    """Read worker identity from the environment."""
    worker_id = os.environ.get("GROK_OBSERVER_AGENT_ID", "")
    worker_token = os.environ.get("GROK_OBSERVER_AGENT_TOKEN", "")
    if not worker_id or not worker_token:
        raise RuntimeError("worker identity is not set (GROK_OBSERVER_AGENT_ID/TOKEN required)")
    port = int(os.environ.get("GROK_OBSERVER_CONTROL_PORT", DEFAULT_PORT))
    return worker_id, worker_token, port


def call_tool(name: str, args: dict) -> dict:
    """Call a worker hub tool over the daemon control socket."""
    worker_id, worker_token, port = identity()
    payload = {
        "action": "worker_hub",
        "args": {
            "worker_id": worker_id,
            "worker_token": worker_token,
            **op_args_for(name, args),
        },
    }
    if name == "wait":
        timeout_seconds = int(args.get("timeout_seconds", 120))
        timeout = max(10, timeout_seconds + 5)
    else:
        timeout = 15
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.settimeout(timeout)
            sock.sendall(data)
            line = sock.makefile("r", encoding="utf-8").readline()
    except OSError as exc:
        raise RuntimeError(f"cannot reach daemon at 127.0.0.1:{port}: {exc}") from exc
    if not line:
        raise RuntimeError("empty reply from daemon")
    try:
        response = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid response from daemon: {exc}") from exc
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "worker hub request failed"))
    data = response.get("data", {})
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        "structuredContent": data,
    }


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
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": VERSION},
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
