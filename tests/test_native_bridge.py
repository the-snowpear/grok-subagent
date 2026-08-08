"""Tests for native_bridge.py: the native Grok MCP stdio transport.

Group A exercises the pure API: the TOOLS declarations, the op_args_for
mapping for all four worker hub tools, and worker identity env validation.
Group B drives native_bridge.py as a real subprocess over stdio JSON-RPC
against a fake daemon control socket, verifying the full MCP roundtrip
(initialize, tools/list, tools/call) plus error paths.
"""

from __future__ import annotations

import json
import os
import queue
import select
import socketserver
import subprocess
import sys
import threading
import unittest
from pathlib import Path

import native_bridge


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "native_bridge.py"

_IDENTITY_ENV_VARS = (
    "GROK_OBSERVER_AGENT_ID",
    "GROK_OBSERVER_AGENT_TOKEN",
    "GROK_OBSERVER_CONTROL_PORT",
    "GROK_OBSERVER_WORKER_CONTROL_PORT",
)

CANNED_PEERS_DATA = {"caller": "w1", "main": "main:T", "peers": [{"id": "w1"}]}

# Payloads received by the fake daemon, guarded by a lock because
# ThreadingTCPServer handles each connection on its own thread.
_RECORDED_PAYLOADS = []
_RECORD_LOCK = threading.Lock()


class _FakeDaemonHandler(socketserver.StreamRequestHandler):
    """Read one JSON-line control request per connection, record it, reply canned."""

    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        payload = json.loads(line.decode("utf-8"))
        with _RECORD_LOCK:
            _RECORDED_PAYLOADS.append(payload)
        response = {"ok": True, "data": CANNED_PEERS_DATA}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()


class _FakeDaemonServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _read_line(proc: subprocess.Popen, timeout: float = 10.0) -> str:
    """Read one line from the subprocess stdout, bounded by ``timeout``.

    Uses select() where the platform supports it; on Windows select() only
    accepts sockets, so fall back to a daemon reader thread feeding a queue
    (also bounded by ``timeout``).
    """
    try:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
    except OSError:
        ready = None
    if ready is not None:
        if not ready:
            raise AssertionError(f"no MCP response within {timeout}s")
        line = proc.stdout.readline()
    else:
        lines: queue.Queue = queue.Queue()

        def _reader() -> None:
            try:
                lines.put(proc.stdout.readline())
            except Exception:
                lines.put("")

        threading.Thread(target=_reader, daemon=True).start()
        try:
            line = lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no MCP response within {timeout}s")
    if not line:
        raise AssertionError("bridge process exited before responding")
    return line


def _restore_env(saved: dict) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class NativeBridgePureTest(unittest.TestCase):
    """Pure API tests: no subprocess, no sockets."""

    def test_tools_declared(self) -> None:
        names = [tool["name"] for tool in native_bridge.TOOLS]
        self.assertEqual(names, ["peers", "send", "inbox", "wait"])
        required = {
            "peers": [],
            "send": ["to", "message"],
            "inbox": [],
            "wait": [],
        }
        by_name = {tool["name"]: tool for tool in native_bridge.TOOLS}
        self.assertEqual(set(by_name), set(required))
        for name in required:
            tool = by_name[name]
            self.assertIsInstance(tool["description"], str)
            self.assertTrue(tool["description"])
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertEqual(schema["required"], required[name])
            self.assertIs(schema["additionalProperties"], False)
        send_schema = by_name["send"]["inputSchema"]
        self.assertEqual(send_schema["properties"]["to"]["type"], "string")
        self.assertEqual(send_schema["properties"]["message"]["type"], "string")
        self.assertIn("reply_to", send_schema["properties"])
        self.assertEqual(by_name["inbox"]["inputSchema"]["properties"]["peek"]["type"], "boolean")
        wait_timeout = by_name["wait"]["inputSchema"]["properties"]["timeout_seconds"]
        self.assertEqual(wait_timeout["default"], 120)
        self.assertEqual(wait_timeout["minimum"], 0)
        self.assertEqual(wait_timeout["maximum"], 300)

    def test_op_args_for_peers(self) -> None:
        self.assertEqual(native_bridge.op_args_for("peers", {}), {"op": "list"})
        self.assertEqual(native_bridge.op_args_for("peers", {"unexpected": 1}), {"op": "list"})

    def test_op_args_for_send(self) -> None:
        self.assertEqual(
            native_bridge.op_args_for("send", {"to": "main:T", "message": "hi"}),
            {"op": "send", "to": "main:T", "message": "hi"},
        )
        self.assertEqual(
            native_bridge.op_args_for("send", {"to": "w2", "message": "m", "reply_to": "r1"}),
            {"op": "send", "to": "w2", "message": "m", "reply_to": "r1"},
        )
        # reply_to=None must be dropped, never forwarded as null.
        self.assertEqual(
            native_bridge.op_args_for("send", {"to": "w2", "message": "m", "reply_to": None}),
            {"op": "send", "to": "w2", "message": "m"},
        )

    def test_op_args_for_inbox(self) -> None:
        self.assertEqual(native_bridge.op_args_for("inbox", {}), {"op": "inbox", "peek": False})
        self.assertEqual(native_bridge.op_args_for("inbox", {"peek": True}), {"op": "inbox", "peek": True})
        self.assertEqual(native_bridge.op_args_for("inbox", {"peek": 0}), {"op": "inbox", "peek": False})

    def test_op_args_for_wait(self) -> None:
        self.assertEqual(
            native_bridge.op_args_for("wait", {}),
            {"op": "wait", "timeout_seconds": 120},
        )
        self.assertEqual(
            native_bridge.op_args_for("wait", {"timeout_seconds": 30}),
            {"op": "wait", "timeout_seconds": 30},
        )
        self.assertEqual(
            native_bridge.op_args_for("wait", {"from": "main:T", "timeout_seconds": 5}),
            {"op": "wait", "from": "main:T", "timeout_seconds": 5},
        )
        # Empty from is falsy and must be omitted; string timeout coerces to int.
        self.assertEqual(
            native_bridge.op_args_for("wait", {"from": "", "timeout_seconds": "45"}),
            {"op": "wait", "timeout_seconds": 45},
        )

    def test_identity_requires_id_and_token(self) -> None:
        saved = {key: os.environ.get(key) for key in _IDENTITY_ENV_VARS}
        try:
            for key in _IDENTITY_ENV_VARS:
                os.environ.pop(key, None)
            with self.assertRaisesRegex(RuntimeError, "worker identity"):
                native_bridge.identity()
        finally:
            _restore_env(saved)

    def test_identity_requires_token_when_id_present(self) -> None:
        saved = {key: os.environ.get(key) for key in _IDENTITY_ENV_VARS}
        try:
            for key in _IDENTITY_ENV_VARS:
                os.environ.pop(key, None)
            os.environ["GROK_OBSERVER_AGENT_ID"] = "w1"
            with self.assertRaisesRegex(RuntimeError, "worker identity"):
                native_bridge.identity()
        finally:
            _restore_env(saved)

    def test_identity_returns_env_triple(self) -> None:
        saved = {key: os.environ.get(key) for key in _IDENTITY_ENV_VARS}
        try:
            for key in _IDENTITY_ENV_VARS:
                os.environ.pop(key, None)
            os.environ["GROK_OBSERVER_AGENT_ID"] = "w1"
            os.environ["GROK_OBSERVER_AGENT_TOKEN"] = "tok"
            # GROK_OBSERVER_WORKER_CONTROL_PORT is the primary worker port var.
            os.environ["GROK_OBSERVER_WORKER_CONTROL_PORT"] = "47831"
            self.assertEqual(native_bridge.identity(), ("w1", "tok", 47831))
            os.environ.pop("GROK_OBSERVER_WORKER_CONTROL_PORT", None)
            # Older daemons only set GROK_OBSERVER_CONTROL_PORT: fall back to it.
            os.environ["GROK_OBSERVER_CONTROL_PORT"] = "47830"
            self.assertEqual(native_bridge.identity(), ("w1", "tok", 47830))
            os.environ.pop("GROK_OBSERVER_CONTROL_PORT", None)
            # Neither set: the worker control default applies.
            self.assertEqual(native_bridge.identity(), ("w1", "tok", 47832))
        finally:
            _restore_env(saved)


class _BridgeProcTestCase(unittest.TestCase):
    """Base class for subprocess-driven tests: spawn + JSON-RPC helpers."""

    def _spawn(self, overrides: dict) -> subprocess.Popen:
        env = os.environ.copy()
        for key in _IDENTITY_ENV_VARS:
            env.pop(key, None)
        env["GROK_OBSERVER_NO_BROWSER"] = "1"
        env.update(overrides)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(
            [sys.executable, str(BRIDGE)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
        )

    def _rpc(
        self,
        proc: subprocess.Popen,
        method: str,
        params: dict | None,
        timeout: float = 10.0,
    ) -> dict:
        self._request_id += 1
        request_id = self._request_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        response = json.loads(_read_line(proc, timeout))
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], request_id)
        return response

    @staticmethod
    def _terminate(proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


class NativeBridgeE2ETest(_BridgeProcTestCase):
    """stdio MCP roundtrip against a fake daemon control socket."""

    def setUp(self) -> None:
        with _RECORD_LOCK:
            _RECORDED_PAYLOADS.clear()
        self.server = _FakeDaemonServer(("127.0.0.1", 0), _FakeDaemonHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.proc = self._spawn(
            {
                "GROK_OBSERVER_AGENT_ID": "w1",
                "GROK_OBSERVER_AGENT_TOKEN": "tok",
                "GROK_OBSERVER_WORKER_CONTROL_PORT": str(self.server.server_address[1]),
            }
        )
        self._request_id = 0

    def tearDown(self) -> None:
        self._terminate(getattr(self, "proc", None))
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            server.server_close()

    def test_stdio_mcp_roundtrip_against_fake_daemon(self) -> None:
        # 1. initialize
        response = self._rpc(self.proc, "initialize", None)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"]["name"], "grok-agent-observer-native")
        self.assertEqual(result["serverInfo"]["version"], "1.0.0")

        # 2. tools/list
        response = self._rpc(self.proc, "tools/list", None)
        tool_names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(tool_names, ["peers", "send", "inbox", "wait"])

        # 3. tools/call peers -> canned data; exactly one daemon payload.
        response = self._rpc(self.proc, "tools/call", {"name": "peers", "arguments": {}})
        self.assertEqual(response["result"]["structuredContent"], CANNED_PEERS_DATA)
        self.assertEqual(
            response["result"]["content"],
            [{"type": "text", "text": json.dumps(CANNED_PEERS_DATA, ensure_ascii=False, indent=2)}],
        )
        with _RECORD_LOCK:
            self.assertEqual(len(_RECORDED_PAYLOADS), 1)
            payload = _RECORDED_PAYLOADS[0]
        # The worker control protocol has no generic action field: worker
        # identity and the hub op sit at the top level of the request.
        self.assertNotIn("action", payload)
        self.assertEqual(payload["worker_id"], "w1")
        self.assertEqual(payload["worker_token"], "tok")
        self.assertEqual(payload["op"], "list")

        # 4. tools/call send -> recorded payload carries op/to/message.
        response = self._rpc(
            self.proc, "tools/call", {"name": "send", "arguments": {"to": "main:T", "message": "hi"}}
        )
        with _RECORD_LOCK:
            self.assertEqual(len(_RECORDED_PAYLOADS), 2)
            send_args = _RECORDED_PAYLOADS[1]
        self.assertNotIn("action", send_args)
        self.assertEqual(send_args["op"], "send")
        self.assertEqual(send_args["to"], "main:T")
        self.assertEqual(send_args["message"], "hi")
        self.assertNotIn("reply_to", send_args)

        # 5. unknown tool -> -32000 error and no daemon traffic.
        response = self._rpc(self.proc, "tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32000)
        self.assertIn("Unknown tool", response["error"]["message"])
        with _RECORD_LOCK:
            self.assertEqual(len(_RECORDED_PAYLOADS), 2)


class NativeBridgeMissingIdentityTest(_BridgeProcTestCase):
    """tools/call without worker identity must surface as an MCP error."""

    def setUp(self) -> None:
        self.proc = self._spawn({})
        self._request_id = 0

    def tearDown(self) -> None:
        self._terminate(getattr(self, "proc", None))

    def test_tools_call_without_identity_returns_mcp_error(self) -> None:
        init = self._rpc(self.proc, "initialize", None)
        self.assertEqual(init["result"]["serverInfo"]["name"], "grok-agent-observer-native")
        response = self._rpc(self.proc, "tools/call", {"name": "peers", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32000)
        self.assertIn("worker identity", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
