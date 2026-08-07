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

SERVER_INSTRUCTIONS = (
    "Use create_agent for delegated Grok work. Immediately after it succeeds, tell the user once the "
    "agent_name, agent_id, status, and a clickable Markdown link using viewer_url. Do not leave the "
    "observer link hidden only inside the MCP tool call. Do not repeat the observer link on later "
    "send, update_agent, result, or signoff turns, and do not include it again in the final answer "
    "unless the user explicitly asks for the link. Use update_agent to steer work "
    "(mode=auto|immediate|tool_boundary; running turns may wait for a tool boundary or interrupt-and-resume). "
    "This is not lossless Interject. Use send only to queue a later turn. Continue with "
    "the same agent_id. Call wait once for completion, inspect result, verify locally, then call "
    "signoff with the verdict."
)


TOOLS = [
    {
        "name": "create_agent",
        "description": (
            "Create an asynchronous observable Grok subagent and return immediately. After this tool "
            "succeeds, tell the user once: agent_name, agent_id, status, and a clickable Markdown "
            "link using viewer_url. Do not hide viewer_url only inside the tool call. Do not repeat "
            "the observer link on later turns unless the user asks. Optional profile: default, fast, "
            "deep, or isolated (deep/isolated run the agent in a dedicated git worktree); worktree and "
            "max_turns override profile defaults."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
                "codex_thread_title": {"type": "string"},
                "profile": {"type": "string"},
                "worktree": {"type": "boolean"},
                "max_turns": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["agent_name", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send",
        "description": (
            "Queue a follow-up prompt in an existing observable Grok subagent conversation. Reuse the "
            "same agent_id. Do not re-send the observer link unless the user asks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}, "prompt": {"type": "string"}},
            "required": ["agent_id", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_agent",
        "description": (
            "Update an existing Grok subagent like Codex steering. Modes: auto (default: tool_boundary "
            "when tools are in-flight, else immediate), immediate (interrupt-and-resume now), "
            "tool_boundary (wait for tool completion, then interrupt; timeout falls back to immediate). "
            "Each call enqueues an independent replacement turn processed in order. This is not Grok "
            "TUI's lossless Interject (lossless_interject is always false). If idle, starts a normal "
            "follow-up. Reuse the same agent_id and observer page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["auto", "immediate", "tool_boundary"],
                    "default": "auto",
                    "description": (
                        "auto: boundary if tools in-flight else immediate. "
                        "immediate: interrupt now. tool_boundary: wait for tool completion."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 30,
                    "description": "tool_boundary wait cap; on expiry apply immediate_timeout.",
                },
            },
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
        "description": (
            "Return only the final Grok result and compact change/test metadata for Codex review. Verify "
            "the work locally, then call signoff. Do not re-send the observer link unless the user asks."
        ),
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
        "description": (
            "Record Codex review after verification: accepted, partial, or rejected. After recording it, "
            "tell the user the verdict. Do not re-send the observer link unless the user asks."
        ),
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
    {
        "name": "hub",
        "description": (
            "Coordinate agents in the current Codex conversation. "
            "Ops: list peers, send a durable message, inspect Main inbox, "
            "or wait for a Main-directed message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["list", "send", "inbox", "wait"],
                },
                "to": {"type": "string"},
                "message": {"type": "string"},
                "reply_to": {"type": "string"},
                "from": {"type": "string"},
                "peek": {"type": "boolean", "default": False},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 300,
                    "default": 120,
                },
            },
            "required": ["op"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_agents",
        "description": (
            "Batch-spawn 1-20 Grok subagents that share the caller's conversation "
            "context (thread, origin, cwd) and can coordinate with each other and "
            "Main through the hub tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_name": {"type": "string"},
                            "prompt": {"type": "string"},
                            "cwd": {"type": "string"},
                            "codex_thread_title": {"type": "string"},
                            "profile": {"type": "string"},
                            "worktree": {"type": "boolean"},
                            "max_turns": {"type": "integer", "minimum": 1, "maximum": 500},
                        },
                        "required": ["agent_name", "prompt"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                    "maxItems": 20,
                },
            },
            "required": ["agents"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_any",
        "description": (
            "Unified wait returning the first of: a new hub mailbox message addressed "
            "to Main, any listed agent reaching a terminal state "
            "(completed/failed/cancelled), or the timeout. Agent-terminal latency is "
            "at most ~0.25s."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "from": {"type": "string"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 120,
                },
            },
            "required": ["agent_ids"],
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

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = data_dir / "daemon.stderr.log"
    # Keep the handle open long enough for the child to inherit the fd.
    stderr_file = open(stderr_path, "a", encoding="utf-8", errors="replace")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [sys.executable, str(DAEMON)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            creationflags=creationflags,
            close_fds=True,
        )
    finally:
        try:
            stderr_file.close()
        except OSError:
            pass
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.15)
        try:
            _request({"action": "ping"}, timeout=1)
            return
        except Exception:
            continue
    # Surface recent daemon stderr for diagnosis when startup times out.
    tail = ""
    try:
        raw = stderr_path.read_bytes()
        tail = raw[-2048:].decode("utf-8", errors="replace").strip()
    except OSError:
        pass
    detail = f"\n--- daemon.stderr.log (tail) ---\n{tail}" if tail else f"\n(no stderr at {stderr_path})"
    raise RuntimeError("Grok Observer daemon did not start" + detail)


def call_tool(name: str, args: dict) -> dict:
    _ensure_daemon()
    payload = {"action": name, "args": args}
    if name in {"create_agent", "hub", "create_agents", "wait_any"}:
        payload["context"] = {
            "codex_thread_id": os.environ.get("CODEX_THREAD_ID", "unknown"),
            "codex_origin": os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex"),
            "cwd": os.getcwd(),
        }
    timeout = min(max(int(args.get("timeout_seconds", 300)), 1), 300) + 5 if name == "wait" else 15
    data = _request(payload, timeout=timeout)
    # Keep the original JSON text first for compatibility with existing model/client behavior.
    result = {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        "structuredContent": data,
    }
    # Link promotion experiment: only create_agent gets one Markdown observer link.
    # Never emit MCP resource_link — Codex sidebars collect those as "Outputs".
    # send / update_agent / result / signoff keep structured JSON only (no extra link text).
    if name == "create_agent":
        agent_id = data.get("agent_id") or data.get("id") or args.get("agent_id")
        viewer_url = data.get("viewer_url")
        if not viewer_url and agent_id:
            state = _state() or {}
            port = state.get("viewer_port")
            if port:
                viewer_url = f"http://127.0.0.1:{int(port)}/#/agents/{agent_id}"
        if viewer_url:
            agent_name = data.get("name") or args.get("agent_name") or "Grok subagent"
            status = data.get("status") or "queued"
            result["content"].append(
                {
                    "type": "text",
                    "text": (
                        "ASSISTANT DISPLAY ACTION: Immediately show this link to the user in your commentary.\n\n"
                        f"Grok subagent: {agent_name}\n"
                        f"agent_id: {agent_id}\n"
                        f"status: {status}\n"
                        f"[View Grok execution]({viewer_url})"
                    ),
                }
            )
    return result


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
                    "instructions": SERVER_INSTRUCTIONS,
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
