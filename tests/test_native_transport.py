"""Tests for native_transport.py: the Grok transport capability probe and
the opt-in worker MCP plugin dir generator.

Group A exercises the probe API: the documented result shape, honesty about
the current ``grok -p`` worker transport (no plugin/MCP flag as of grok
1.0.0), and the plugin dir generator's placement (only under the requested
observer data dir, never the project root or ~/.grok). Group B validates a
generated plugin dir with the real ``grok plugin validate`` command and then
drives the plugin's MCP command (native_bridge.py) as a real subprocess over
stdio JSON-RPC against a fake daemon control socket, proving the generated
plugin actually serves the bridge's tools.
"""

from __future__ import annotations

import json
import os
import queue
import select
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import native_transport


ROOT = Path(__file__).resolve().parents[1]

# The bridge subprocess needs a full worker identity for tools/call; the
# probe itself never touches these. They are copied out of the parent env so
# the test never mutates the runner's environment.
_IDENTITY_ENV_VARS = (
    "GROK_OBSERVER_AGENT_ID",
    "GROK_OBSERVER_AGENT_TOKEN",
    "GROK_OBSERVER_CONTROL_PORT",
    "GROK_OBSERVER_WORKER_CONTROL_PORT",
)

CANNED_PEERS_DATA = {"caller": "w1", "main": "main:T", "peers": [{"id": "w1"}]}


class _FakeDaemonHandler(socketserver.StreamRequestHandler):
    """Read one JSON-line control request per connection, reply canned."""

    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
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


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


class NativeTransportProbeTest(unittest.TestCase):
    """Probe API shape and honesty about the current grok worker transport."""

    def test_probe_shape(self) -> None:
        result = native_transport.probe_grok_transport()
        self.assertEqual(
            set(result),
            {
                "version",
                "binary_found",
                "agent_stdio_plugin_dir_supported",
                "prompt_mode_plugin_supported",
                "injection",
                "fallback",
                "note",
            },
        )
        self.assertIsInstance(result["binary_found"], bool)
        self.assertIsInstance(result["agent_stdio_plugin_dir_supported"], bool)
        self.assertIsInstance(result["prompt_mode_plugin_supported"], bool)
        self.assertIn(result["injection"], {"automatic", "unavailable"})
        self.assertEqual(result["fallback"], "grok_hub CLI")
        self.assertIsInstance(result["note"], str)
        self.assertTrue(result["note"])

    def test_probe_finds_grok_or_absent(self) -> None:
        result = native_transport.probe_grok_transport()
        if shutil.which("grok") is None:
            self.assertFalse(result["binary_found"])
            self.assertIsNone(result["version"])
        else:
            self.assertTrue(result["binary_found"])
            self.assertIsInstance(result["version"], str)
            self.assertTrue(result["version"].startswith("grok"))

    def test_probe_honest_on_prompt_mode(self) -> None:
        result = native_transport.probe_grok_transport()
        # The injection flag must never diverge from the observed capability.
        self.assertIsInstance(result["prompt_mode_plugin_supported"], bool)
        self.assertEqual(
            result["injection"],
            "automatic" if result["prompt_mode_plugin_supported"] else "unavailable",
        )
        if shutil.which("grok") is not None:
            # Current grok 1.0.0: `grok -p` (the daemon worker transport) has
            # no --plugin-dir/MCP flag. If a future grok adds one, this
            # assertion is the one to revisit.
            self.assertFalse(result["prompt_mode_plugin_supported"])


class NativeTransportPluginDirTest(unittest.TestCase):
    """The generated plugin dir: content, placement, and real grok validation."""

    def test_write_plugin_dir_creates_mcp_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            target = native_transport.write_worker_plugin_dir("w1", base_dir)
            self.assertEqual(target, base_dir / "w1")
            plugin_path = base_dir / "w1" / ".mcp.json"
            self.assertTrue(plugin_path.is_file())
            plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
            grok_hub = plugin["mcpServers"]["grok-hub"]
            self.assertEqual(grok_hub["command"], sys.executable)
            self.assertEqual(len(grok_hub["args"]), 1)
            self.assertIn("native_bridge.py", grok_hub["args"][0])
            self.assertTrue(Path(grok_hub["args"][0]).is_absolute())

    def test_write_plugin_dir_no_global_or_project_mutation(self) -> None:
        home_config = Path.home() / ".grok" / "config.toml"
        mtime_before = os.path.getmtime(home_config) if home_config.is_file() else None

        before = set(os.listdir(ROOT))
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            target = native_transport.write_worker_plugin_dir("w2", base_dir)
            # Everything created must live under the requested base dir.
            created = [p for p in base_dir.rglob("*") if p.is_file()]
            self.assertTrue(created)
            for path in created:
                self.assertTrue(path.resolve().is_relative_to(base_dir.resolve()))
            self.assertTrue((target / ".mcp.json").is_file())
        # The repo root must not gain a project .mcp.json or .grok dir.
        new_entries = set(os.listdir(ROOT)) - before
        self.assertNotIn(".mcp.json", new_entries)
        self.assertNotIn(".grok", new_entries)
        if "data" not in before:
            self.assertNotIn("data", new_entries)
        # The global grok config must be untouched.
        if mtime_before is not None:
            self.assertEqual(os.path.getmtime(home_config), mtime_before)

    def test_plugin_dir_validates_with_real_grok(self) -> None:
        if shutil.which("grok") is None:
            self.skipTest("grok binary not installed")
        with tempfile.TemporaryDirectory() as tmp:
            target = native_transport.write_worker_plugin_dir("wv", Path(tmp))
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(
                ["grok", "plugin", "validate", str(target)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=creationflags,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"grok plugin validate failed: {result.stdout}\n{result.stderr}",
            )


class NativeTransportPluginE2ETest(unittest.TestCase):
    """The generated plugin command must actually serve the bridge's MCP tools."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.target = native_transport.write_worker_plugin_dir("w1", Path(self.tmp.name))
        plugin = json.loads((self.target / ".mcp.json").read_text(encoding="utf-8"))
        grok_hub = plugin["mcpServers"]["grok-hub"]

        self.server = _FakeDaemonServer(("127.0.0.1", 0), _FakeDaemonHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        env = os.environ.copy()
        for key in _IDENTITY_ENV_VARS:
            env.pop(key, None)
        env["GROK_OBSERVER_AGENT_ID"] = "w1"
        env["GROK_OBSERVER_AGENT_TOKEN"] = "tok"
        env["GROK_OBSERVER_WORKER_CONTROL_PORT"] = str(self.server.server_address[1])
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [grok_hub["command"], *grok_hub["args"]],
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
        self._request_id = 0

    def tearDown(self) -> None:
        _terminate(getattr(self, "proc", None))
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            server.server_close()
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def _rpc(self, method: str, params: dict | None, timeout: float = 10.0) -> dict:
        self._request_id += 1
        request = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        response = json.loads(_read_line(self.proc, timeout))
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], self._request_id)
        return response

    def test_plugin_mcp_command_serves_bridge(self) -> None:
        # initialize -> the bridge's server identity.
        response = self._rpc("initialize", None)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "grok-agent-observer-native")
        # tools/list -> the four worker hub tools.
        response = self._rpc("tools/list", None)
        tool_names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(tool_names, ["peers", "send", "inbox", "wait"])


class NativeTransportCliTest(unittest.TestCase):
    """The probe subcommand must print a parseable capability report."""

    def test_cli_probe_runs(self) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(
            [sys.executable, str(ROOT / "native_transport.py"), "probe"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            creationflags=creationflags,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("binary_found", report)


if __name__ == "__main__":
    unittest.main()
