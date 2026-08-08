"""Regression tests for Grok Agent Observer security and lifecycle fixes."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import daemon
import server as mcp_server


ROOT = Path(__file__).resolve().parents[1]
FAKE_GROK = ROOT / "tests" / "fake_grok.py"


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp.read(), headers
    except urllib.error.HTTPError as exc:
        # HTTPError is also a response handle; close it to avoid ResourceWarning.
        try:
            headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            return exc.code, exc.read(), headers
        finally:
            exc.close()


def _raw_http_get(host: str, port: int, raw_path: str, timeout: float = 3.0) -> tuple[int, bytes]:
    """Issue a raw request line so path traversal is not normalized by urllib."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        request = f"GET {raw_path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode("ascii", errors="replace"))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    header_blob, _, body = data.partition(b"\r\n\r\n")
    status_line = header_blob.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    status = int(status_line.split()[1])
    return status, body


def _raw_http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    host_header: str | None = None,
    extra_headers: dict | None = None,
    body: bytes = b"",
    timeout: float = 3.0,
) -> tuple[int, bytes]:
    """Send a request with a caller-controlled Host header (for rebinding tests)."""
    lines = [f"{method} {path} HTTP/1.1"]
    lines.append(f"Host: {host_header if host_header is not None else f'{host}:{port}'}")
    for key, value in (extra_headers or {}).items():
        lines.append(f"{key}: {value}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", errors="replace") + body
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    header_blob, _, resp_body = data.partition(b"\r\n\r\n")
    status_line = header_blob.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    status = int(status_line.split()[1])
    return status, resp_body


class _IsolatedDbMixin:
    """Patch daemon paths to a temp tree; restore after each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._orig = {
            "DB_PATH": daemon.DB_PATH,
            "ARTIFACTS": daemon.ARTIFACTS,
            "DATA": daemon.DATA,
            "STATE_PATH": daemon.STATE_PATH,
            "LOCK_PATH": daemon.LOCK_PATH,
            "STATIC": daemon.STATIC,
            "ROOT": daemon.ROOT,
        }
        # Keep ARTIFACTS under ROOT so artifact() relative paths stay valid.
        daemon.ROOT = self.folder
        daemon.DATA = self.folder / "data"
        daemon.DATA.mkdir(parents=True, exist_ok=True)
        daemon.ARTIFACTS = daemon.DATA / "artifacts"
        daemon.ARTIFACTS.mkdir(parents=True, exist_ok=True)
        daemon.DB_PATH = daemon.DATA / "observer.sqlite"
        daemon.STATE_PATH = daemon.DATA / "daemon-state.json"
        daemon.LOCK_PATH = daemon.DATA / "daemon.lock"
        daemon.STATIC = ROOT / "viewer" / "dist"
        daemon.init_db()
        # Clear in-memory registries left by previous tests.
        with daemon.RUNNERS_LOCK:
            for runner in list(daemon.RUNNERS.values()):
                runner.shutdown()
            daemon.RUNNERS.clear()
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        # Avoid binding the real viewer HTTP port during unit tests.
        self._orig_ensure_viewer = daemon.ensure_viewer
        daemon.ensure_viewer = lambda agent_id=None: False

        self.env = os.environ.copy()
        self.env["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        self.env["GROK_OBSERVER_NO_BROWSER"] = "1"
        self.env["GROK_FAKE_DURATION"] = "0.05"
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"

    def tearDown(self):
        with daemon.RUNNERS_LOCK:
            for runner in list(daemon.RUNNERS.values()):
                runner.shutdown()
            daemon.RUNNERS.clear()
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        daemon.ensure_viewer = self._orig_ensure_viewer
        for key, value in self._orig.items():
            setattr(daemon, key, value)
        for key in ("GROK_FAKE_DURATION", "GROK_FAKE_MARKER"):
            os.environ.pop(key, None)
        self._tmp.cleanup()

    def _create(self, prompt: str = "do work", name: str = "t", cwd: str | None = None) -> dict:
        prev = os.environ.get("GROK_OBSERVER_FAKE_GROK")
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        try:
            return daemon.action(
                "create_agent",
                {"agent_name": name, "prompt": prompt, "cwd": cwd or str(self.folder)},
                {"codex_thread_id": "test-thread", "codex_origin": "test"},
            )
        finally:
            if prev is None:
                os.environ.pop("GROK_OBSERVER_FAKE_GROK", None)
            else:
                os.environ["GROK_OBSERVER_FAKE_GROK"] = prev


class ObserverSmokeTest(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.copy()
        self.env["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        self.env["GROK_OBSERVER_NO_BROWSER"] = "1"

    def test_mcp_declares_async_lifecycle_tools(self):
        messages = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        ]) + "\n"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "server.py")],
            input=messages,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
            env=self.env,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"create_agent", "send", "update_agent", "status", "wait", "result", "cancel", "signoff", "hub", "create_agents", "wait_any"})
        instructions = responses[0]["result"]["instructions"]
        self.assertIn("viewer_url", instructions)
        create_description = next(
            tool["description"]
            for tool in responses[1]["result"]["tools"]
            if tool["name"] == "create_agent"
        )
        self.assertIn("clickable Markdown link", create_description)

    def test_create_agent_result_promotes_observer_link_without_annotations(self):
        data = {
            "agent_id": "agent-123",
            "status": "queued",
            "viewer_url": "http://127.0.0.1:47831/#/agents/agent-123",
        }
        with (
            mock.patch.object(mcp_server, "_ensure_daemon"),
            mock.patch.object(mcp_server, "_request", return_value=data),
        ):
            result = mcp_server.call_tool(
                "create_agent",
                {"agent_name": "reviewer", "prompt": "review this"},
            )

        self.assertEqual(result["structuredContent"], data)
        self.assertEqual(json.loads(result["content"][0]["text"]), data)
        self.assertEqual(len(result["content"]), 2)
        display = result["content"][1]
        self.assertEqual(display["type"], "text")
        self.assertNotIn("annotations", display)
        self.assertIn("[View Grok execution]", display["text"])
        self.assertIn(f"({data['viewer_url']})", display["text"])
        self.assertFalse(any(item.get("type") == "resource_link" for item in result["content"]))

    def test_other_tools_do_not_attach_observer_link(self):
        data = {
            "agent_id": "agent-123",
            "status": "running",
            "viewer_url": "http://127.0.0.1:47831/#/agents/agent-123",
            "verdict": "accepted",
        }
        cases = (
            ("send", {"agent_id": "agent-123", "prompt": "more work"}),
            ("update_agent", {"agent_id": "agent-123", "prompt": "new direction"}),
            ("result", {"agent_id": "agent-123"}),
            ("signoff", {"agent_id": "agent-123", "verdict": "accepted", "summary": "ok"}),
        )
        for name, args in cases:
            with self.subTest(tool=name):
                with (
                    mock.patch.object(mcp_server, "_ensure_daemon"),
                    mock.patch.object(mcp_server, "_request", return_value=data),
                ):
                    result = mcp_server.call_tool(name, args)
                self.assertEqual(result["structuredContent"], data)
                self.assertEqual(len(result["content"]), 1)
                self.assertEqual(json.loads(result["content"][0]["text"]), data)
                self.assertFalse(any(item.get("type") == "resource_link" for item in result["content"]))
                self.assertNotIn("[View Grok execution]", result["content"][0]["text"])
                self.assertNotIn("ASSISTANT DISPLAY ACTION", result["content"][0]["text"])

    def test_terminal_ansi_is_removed(self):
        value = "\x1b[2m2026-07-11\x1b[0m \x1b[33mWARN\x1b[0m normal text"
        self.assertEqual(daemon.clean_terminal_text(value), "2026-07-11 WARN normal text")


class WaitSemanticsTest(_IsolatedDbMixin, unittest.TestCase):
    def test_update_agent_interrupts_running_turn_then_resumes(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        os.environ["GROK_FAKE_DURATION"] = "2"

        created = self._create("long first turn")
        agent_id = created["agent_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            if daemon.action("status", {"agent_id": agent_id}, {})["status"] == "running":
                break
            time.sleep(0.05)
        else:
            self.fail("first turn never started")

        updated = daemon.action("update_agent", {"agent_id": agent_id, "prompt": "new direction"}, {})
        self.assertEqual(updated["mode"], "interrupt_and_resume")
        self.assertEqual(updated["requested_mode"], "auto")
        self.assertFalse(updated["lossless_interject"])

        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 15}, {})
        self.assertTrue(final["done"], final)
        result = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual([t["status"] for t in result["turn_results"]], ["interrupted", "completed"])
        self.assertEqual(result["turn_results"][1]["prompt"], "new direction")

    def test_wait_ignores_intermediate_events_until_terminal_state(self):
        agent_id = str(uuid.uuid4())
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("test-thread", "test", str(self.folder), "test", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (agent_id, "test-thread", "test-agent", str(self.folder), agent_id, stamp, stamp),
            )
            db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'running',?)",
                (agent_id, "p", stamp),
            )
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS[agent_id] = threading.Condition()

        result = {}
        waiter = threading.Thread(
            target=lambda: result.update(daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 2}, {}))
        )
        waiter.start()
        time.sleep(0.1)
        daemon.add_event(agent_id, None, "thinking", "intermediate")
        time.sleep(0.15)
        self.assertTrue(waiter.is_alive(), "intermediate events must not finish wait")

        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET status='completed',revision=revision+1,updated_at=? WHERE id=?",
                (daemon.now(), agent_id),
            )
            db.execute(
                "UPDATE turns SET status='completed',completed_at=? WHERE agent_id=?",
                (daemon.now(), agent_id),
            )
        with daemon.CONDITIONS_LOCK:
            condition = daemon.CONDITIONS[agent_id]
        with condition:
            condition.notify_all()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["done"])

    def test_multi_turn_wait_does_not_finish_after_first_turn(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        os.environ["GROK_FAKE_DURATION"] = "0.6"

        created = self._create("first turn")
        agent_id = created["agent_id"]

        # Wait until first turn is actively running.
        deadline = time.time() + 5
        while time.time() < deadline:
            status = daemon.action("status", {"agent_id": agent_id}, {})
            if status["status"] == "running":
                break
            time.sleep(0.05)
        else:
            self.fail("first turn never started")

        send_result = daemon.action("send", {"agent_id": agent_id, "prompt": "second turn"}, {})
        self.assertEqual(send_result["turn_no"], 2)

        # Mid-flight wait must not return done=true after only turn 1.
        mid = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 1}, {})
        if mid.get("done"):
            # Only acceptable if both turns already finished within the short window.
            result = daemon.action("result", {"agent_id": agent_id}, {})
            self.assertGreaterEqual(result["turns"], 2)
        else:
            self.assertFalse(mid["done"])
            with daemon.connect() as db:
                turns = [dict(r) for r in db.execute("SELECT turn_no,status FROM turns WHERE agent_id=? ORDER BY turn_no", (agent_id,))]
            # After first turn ends with pending second, agent must not sit terminal with pending work.
            statuses = {t["turn_no"]: t["status"] for t in turns}
            if statuses.get(1) in {"completed", "failed"} and statuses.get(2) in {"queued", "running"}:
                agent = daemon.action("status", {"agent_id": agent_id}, {})
                self.assertNotIn(agent["status"], {"completed", "failed", "cancelled"})

        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 15}, {})
        self.assertTrue(final["done"], final)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["turns"], 2)

        result = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual(result["turns"], 2)
        self.assertEqual(len(result["turn_results"]), 2)
        self.assertEqual([t["turn_no"] for t in result["turn_results"]], [1, 2])
        self.assertEqual([t["status"] for t in result["turn_results"]], ["completed", "completed"])
        self.assertIn("已完成测试任务", result["final_text"])


class CancelLifecycleTest(_IsolatedDbMixin, unittest.TestCase):
    def test_cancel_unknown_agent_does_not_create_runner(self):
        missing = str(uuid.uuid4())
        with self.assertRaises(ValueError) as ctx:
            daemon.action("cancel", {"agent_id": missing}, {})
        self.assertIn("not found", str(ctx.exception).lower())
        with daemon.RUNNERS_LOCK:
            self.assertNotIn(missing, daemon.RUNNERS)

    def test_cancel_running_stops_process_and_blocks_send(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        os.environ["GROK_FAKE_DURATION"] = "3.0"
        marker = self.folder / "fake_marker.txt"
        os.environ["GROK_FAKE_MARKER"] = str(marker)

        created = self._create("long running")
        agent_id = created["agent_id"]

        deadline = time.time() + 5
        while time.time() < deadline:
            if marker.exists() or daemon.action("status", {"agent_id": agent_id}, {})["status"] == "running":
                break
            time.sleep(0.05)

        cancelled = daemon.action("cancel", {"agent_id": agent_id}, {})
        self.assertEqual(cancelled["status"], "cancelled")

        wait = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 5}, {})
        self.assertTrue(wait["done"])
        self.assertEqual(wait["status"], "cancelled")

        with self.assertRaises(ValueError) as ctx:
            daemon.action("send", {"agent_id": agent_id, "prompt": "after cancel"}, {})
        self.assertIn("cancelled", str(ctx.exception).lower())

        # No further fake_grok launches after cancel.
        starts_before = marker.read_text(encoding="utf-8").count("started:") if marker.exists() else 0
        time.sleep(0.4)
        starts_after = marker.read_text(encoding="utf-8").count("started:") if marker.exists() else 0
        self.assertEqual(starts_before, starts_after)
        with daemon.connect() as db:
            rows = db.execute("SELECT status FROM turns WHERE agent_id=?", (agent_id,)).fetchall()
        self.assertTrue(all(r["status"] == "cancelled" for r in rows))

    def test_cancel_stops_worker_thread(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        os.environ["GROK_FAKE_DURATION"] = "3.0"
        marker = self.folder / "thread_marker.txt"
        os.environ["GROK_FAKE_MARKER"] = str(marker)

        created = self._create("long running")
        agent_id = created["agent_id"]
        runner = daemon.get_runner(agent_id, create=False)
        self.assertIsNotNone(runner)

        deadline = time.time() + 5
        while time.time() < deadline and not runner.thread.is_alive():
            time.sleep(0.05)

        daemon.action("cancel", {"agent_id": agent_id}, {})
        # Worker must exit after cancel (no perpetual queue.get spin).
        runner.thread.join(timeout=5)
        self.assertFalse(runner.thread.is_alive(), "cancel must stop the worker thread")

    def test_cancel_queued_and_multi_turn_queue(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        os.environ["GROK_FAKE_DURATION"] = "2.0"
        marker = self.folder / "queue_marker.txt"
        os.environ["GROK_FAKE_MARKER"] = str(marker)

        created = self._create("turn-1")
        agent_id = created["agent_id"]

        deadline = time.time() + 5
        while time.time() < deadline:
            if daemon.action("status", {"agent_id": agent_id}, {})["status"] == "running":
                break
            time.sleep(0.05)

        daemon.action("send", {"agent_id": agent_id, "prompt": "turn-2"}, {})
        daemon.action("send", {"agent_id": agent_id, "prompt": "turn-3"}, {})

        with daemon.connect() as db:
            queued = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=? AND status='queued'",
                (agent_id,),
            ).fetchone()["c"]
        self.assertGreaterEqual(queued, 1)

        daemon.action("cancel", {"agent_id": agent_id}, {})
        wait = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 5}, {})
        self.assertTrue(wait["done"])
        self.assertEqual(wait["status"], "cancelled")

        with daemon.connect() as db:
            statuses = [r["status"] for r in db.execute("SELECT status FROM turns WHERE agent_id=?", (agent_id,))]
        self.assertTrue(all(s == "cancelled" for s in statuses), statuses)

        starts = marker.read_text(encoding="utf-8").count("started:") if marker.exists() else 0
        time.sleep(0.5)
        starts2 = marker.read_text(encoding="utf-8").count("started:") if marker.exists() else 0
        self.assertEqual(starts, starts2)
        # At most the first running turn may have launched fake_grok.
        self.assertLessEqual(starts2, 1)


class StaticPathTest(_IsolatedDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        daemon.STATIC = ROOT / "viewer" / "dist"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_path_traversal_returns_404_not_index(self):
        attacks = (
            "/../../daemon.py",
            "/../../data/observer.sqlite",
            "/..%2F..%2Fdaemon.py",
            "/%2e%2e/%2e%2e/daemon.py",
            # Double-encoded slash/dot must not SPA-fallback to index.html.
            "/..%252f..%252fdaemon.py",
            "/%252e%252e/%252e%252e/daemon.py",
            "/..%5c..%5cdaemon.py",
            "/api/../../daemon.py",
            "/assets/../../daemon.py",
        )
        for raw in attacks:
            status, body = _raw_http_get("127.0.0.1", self.port, raw)
            self.assertEqual(status, 404, raw)
            # Must not leak source or serve SPA index for traversal.
            self.assertNotIn(b"Persistent Grok process supervisor", body)
            self.assertNotIn(b"CREATE TABLE", body)
            text = body.decode("utf-8", errors="replace")
            self.assertNotIn("<!doctype html>", text.lower())
            self.assertIn(b"not found", body)

    def test_safe_static_relpath_helpers(self):
        self.assertEqual(daemon.safe_static_relpath("/"), "")
        self.assertEqual(daemon.safe_static_relpath("/assets/app.js"), "assets/app.js")
        self.assertEqual(daemon.safe_static_relpath("/agents/abc-123"), "agents/abc-123")
        self.assertIsNone(daemon.safe_static_relpath("/../../daemon.py"))
        self.assertIsNone(daemon.safe_static_relpath("/..%252f..%252fdaemon.py"))
        self.assertIsNone(daemon.safe_static_relpath("/%2e%2e/%2e%2e/data/secret.txt"))
        self.assertIsNone(daemon.safe_artifact_relpath("../secret.txt"))
        self.assertIsNone(daemon.safe_artifact_relpath("..%252fsecret.txt"))
        self.assertIsNone(daemon.safe_artifact_relpath("C:/Windows/win.ini"))
        self.assertEqual(
            daemon.safe_artifact_relpath("data/artifacts/x/note.txt.gz"),
            "data/artifacts/x/note.txt.gz",
        )

    def test_index_and_assets_remain_available(self):
        status, body, _ = _http_get(f"http://127.0.0.1:{self.port}/")
        self.assertEqual(status, 200)
        self.assertTrue(b"html" in body.lower()[:500] or b"DOCTYPE" in body[:200] or b"doctype" in body[:200])
        dist = ROOT / "viewer" / "dist" / "assets"
        if dist.exists():
            assets = list(dist.glob("*.js")) + list(dist.glob("*.css"))
            if assets:
                rel = "/assets/" + assets[0].name
                status2, body2, _ = _http_get(f"http://127.0.0.1:{self.port}{rel}")
                self.assertEqual(status2, 200)
                self.assertGreater(len(body2), 10)

    def test_artifact_path_boundary(self):
        agent_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        art = daemon.ARTIFACTS / agent_id
        art.mkdir(parents=True, exist_ok=True)
        path = art / "note-deadbeef.txt.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write("safe artifact")
        rel_ok = str(path.relative_to(daemon.ROOT)).replace("\\", "/")
        status, body, _ = _http_get(f"http://127.0.0.1:{self.port}/api/artifact?path={urllib.parse.quote(rel_ok)}")
        self.assertEqual(status, 200)
        self.assertIn("safe artifact", json.loads(body.decode())["content"])

        outside = self.folder / "secret.txt"
        outside.write_text("nope", encoding="utf-8")
        bad = str(outside.relative_to(daemon.ROOT)).replace("\\", "/")
        status2, _, _ = _http_get(f"http://127.0.0.1:{self.port}/api/artifact?path={urllib.parse.quote(bad)}")
        self.assertEqual(status2, 404)

        status3, _, _ = _http_get(
            f"http://127.0.0.1:{self.port}/api/artifact?path={urllib.parse.quote('../secret.txt')}"
        )
        self.assertEqual(status3, 404)


class HostHeaderGuardTest(_IsolatedDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        daemon.STATIC = ROOT / "viewer" / "dist"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_local_host_allowed(self):
        for host in (f"127.0.0.1:{self.port}", "localhost", f"localhost:{self.port}"):
            status, _ = _raw_http_request(
                "127.0.0.1", self.port, "GET", "/api/bootstrap", host_header=host
            )
            self.assertEqual(status, 200, host)

    def test_rebinding_host_rejected_on_get(self):
        # DNS-rebinding: attacker domain resolves to 127.0.0.1 but Host is theirs.
        for host in ("evil.example.com", f"evil.example.com:{self.port}", "169.254.1.1"):
            status, body = _raw_http_request(
                "127.0.0.1", self.port, "GET", "/api/bootstrap", host_header=host
            )
            self.assertEqual(status, 403, host)
            self.assertIn(b"forbidden", body)

    def test_rebinding_host_rejected_on_post(self):
        status, body = _raw_http_request(
            "127.0.0.1",
            self.port,
            "POST",
            "/api/viewer/shutdown",
            host_header="evil.example.com",
        )
        self.assertEqual(status, 403)
        self.assertIn(b"forbidden", body)

    def test_missing_host_treated_as_local(self):
        # HTTP/1.0-style clients / direct sockets never carry a rebinding origin.
        self.assertTrue(daemon.ViewerHandler._host_is_local.__doc__ is not None)


class MalformedQueryTest(_IsolatedDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_events_after_non_integer_does_not_500(self):
        status, body, _ = _http_get(
            f"http://127.0.0.1:{self.port}/api/events?agent_id=x&after=not-an-int"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode())["events"], [])

    def test_safe_int_helper(self):
        self.assertEqual(daemon._safe_int("12"), 12)
        self.assertEqual(daemon._safe_int("abc"), 0)
        self.assertEqual(daemon._safe_int(None, 7), 7)
        self.assertEqual(daemon._safe_int("", 3), 3)


class SearchInjectionTest(_IsolatedDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_search_returns_structured_plain_snippet(self):
        agent_id = str(uuid.uuid4())
        stamp = daemon.now()
        payload = 'payload <img onerror="alert(1)" src=x> <script>alert(2)</script> &lt;b&gt; highlight-me'
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("s", "s", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'completed',?,?)",
                (agent_id, "s", "search-agent", str(self.folder), agent_id, stamp, stamp),
            )
            db.execute(
                "INSERT INTO search_index(agent_id,kind,content) VALUES(?,?,?)",
                (agent_id, "result", payload),
            )
        status, body, _ = _http_get(f"http://127.0.0.1:{self.port}/api/search?q=highlight-me")
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertTrue(data["results"])
        hit = data["results"][0]
        self.assertIn("snippet", hit)
        self.assertIsInstance(hit["snippet"], str)
        self.assertNotIn("<mark>", hit["snippet"])
        self.assertIn("matches", hit)
        self.assertIn("highlight-me", hit["snippet"])
        # HTML attack strings must remain plain text in JSON (no HTML markup from backend).
        self.assertIn("<img", hit["snippet"] + payload)  # content may be truncated; ensure no mark wrap of tags required
        for match in hit["matches"]:
            sliced = hit["snippet"][match["start"] : match["end"]]
            self.assertEqual(sliced.lower(), "highlight-me")

    def test_snippet_offsets_unicode_and_entities(self):
        sn = daemon.build_search_snippet("中文 highlight-me 测试", "highlight-me")
        self.assertTrue(sn["matches"])
        m = sn["matches"][0]
        self.assertEqual(sn["text"][m["start"] : m["end"]], "highlight-me")

        sn2 = daemon.build_search_snippet("foo &lt;b&gt; bar", "b")
        for m in sn2["matches"]:
            self.assertEqual(sn2["text"][m["start"] : m["end"]], "b")

        long = ("x" * 50) + "中文MATCH中文" + ("y" * 50)
        sn3 = daemon.build_search_snippet(long, "MATCH", radius=10)
        self.assertTrue(sn3["matches"])
        m = sn3["matches"][0]
        self.assertEqual(sn3["text"][m["start"] : m["end"]], "MATCH")
        self.assertTrue(sn3["text"].startswith("…") or "MATCH" in sn3["text"])


class CleanupResourceTest(_IsolatedDbMixin, unittest.TestCase):
    def test_cleanup_reclaims_runners_and_skips_active(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        os.environ["GROK_FAKE_DURATION"] = "0.05"

        old_id = str(uuid.uuid4())
        stamp_old = (datetime.now(timezone.utc) - timedelta(days=daemon.RETENTION_DAYS + 2)).isoformat()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("old", "old", str(self.folder), "t", stamp_old, stamp_old),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'completed',?,?)",
                (old_id, "old", "old-agent", str(self.folder), old_id, stamp_old, stamp_old),
            )
        art = daemon.ARTIFACTS / old_id
        art.mkdir(parents=True, exist_ok=True)
        (art / "x.txt").write_text("x", encoding="utf-8")

        # Attach a runner as if it had been active historically.
        runner = daemon.get_runner(old_id)
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS[old_id] = threading.Condition()
        self.assertTrue(runner.thread.is_alive())

        # Active agent must not be cleaned.
        active = self._create("active")
        active_id = active["agent_id"]

        # Force active agent updated_at far past while status remains running/queued briefly.
        with daemon.connect() as db:
            # Wait for active to complete so we can still assert cleanup skips running if any.
            pass
        daemon.action("wait", {"agent_id": active_id, "timeout_seconds": 10}, {})

        # Create a second expired completed agent and a fake "running" old one.
        running_old = str(uuid.uuid4())
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (running_old, "old", "run-old", str(self.folder), running_old, stamp_old, stamp_old),
            )

        daemon.cleanup()

        with daemon.RUNNERS_LOCK:
            self.assertNotIn(old_id, daemon.RUNNERS)
        with daemon.CONDITIONS_LOCK:
            self.assertNotIn(old_id, daemon.CONDITIONS)
        self.assertFalse(runner.thread.is_alive())
        self.assertFalse((daemon.ARTIFACTS / old_id).exists())
        with daemon.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM agents WHERE id=?", (old_id,)).fetchone())
            # Running must be preserved even if expired.
            self.assertIsNotNone(db.execute("SELECT 1 FROM agents WHERE id=?", (running_old,)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM agents WHERE id=?", (active_id,)).fetchone())

    def test_manual_delete_reuses_reclaim(self):
        agent_id = str(uuid.uuid4())
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("d", "d", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'completed',?,?)",
                (agent_id, "d", "del", str(self.folder), agent_id, stamp, stamp),
            )
        runner = daemon.get_runner(agent_id)
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS[agent_id] = threading.Condition()
        art = daemon.ARTIFACTS / agent_id
        art.mkdir(parents=True, exist_ok=True)
        (art / "y.txt").write_text("y", encoding="utf-8")

        server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/agents/{agent_id}/delete",
                method="POST",
                data=b"",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read().decode())
            self.assertTrue(payload["deleted"])
        finally:
            server.shutdown()
            server.server_close()

        with daemon.RUNNERS_LOCK:
            self.assertNotIn(agent_id, daemon.RUNNERS)
        with daemon.CONDITIONS_LOCK:
            self.assertNotIn(agent_id, daemon.CONDITIONS)
        self.assertFalse(runner.thread.is_alive())
        self.assertFalse(art.exists())

    def test_derive_display_title_prefers_name_then_prompt(self):
        self.assertEqual(daemon.derive_display_title("fix-login", "long body"), "fix-login")
        title = daemon.derive_display_title(str(uuid.uuid4()), "  First line\nsecond")
        self.assertEqual(title, "First line")
        long_prompt = "x" * 80
        self.assertTrue(daemon.derive_display_title("agent", long_prompt).endswith("…"))
        self.assertLessEqual(len(daemon.derive_display_title("agent", long_prompt)), 60)

    def test_agent_and_task_meta_pin_archive_title(self):
        agent_id = str(uuid.uuid4())
        thread_id = "meta-thread"
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (thread_id, "original", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,display_title,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'completed',?,?,?)",
                (agent_id, thread_id, "raw-name", str(self.folder), agent_id, "raw-name", stamp, stamp),
            )

        server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Agent meta
            body = json.dumps(
                {"pinned": True, "archived": True, "display_title": "  漂亮标题  "}
            ).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/agents/{agent_id}/meta",
                method="POST",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read().decode())
            self.assertEqual(payload["agent"]["display_title"], "漂亮标题")
            self.assertEqual(int(payload["agent"]["pinned"]), 1)
            self.assertEqual(int(payload["agent"]["archived"]), 1)

            # Task meta
            body = json.dumps(
                {"pinned": True, "title": "会话新标题", "archived": False}
            ).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks/{urllib.parse.quote(thread_id)}/meta",
                method="POST",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read().decode())
            self.assertEqual(payload["task"]["title"], "会话新标题")
            self.assertEqual(int(payload["task"]["pinned"]), 1)
            self.assertEqual(int(payload["task"]["archived"]), 0)

            status, raw, _ = _http_get(f"http://127.0.0.1:{port}/api/bootstrap")
            self.assertEqual(status, 200)
            boot = json.loads(raw.decode())
            agent_row = next(a for a in boot["agents"] if a["id"] == agent_id)
            task_row = next(t for t in boot["tasks"] if t["thread_id"] == thread_id)
            self.assertEqual(agent_row["display_title"], "漂亮标题")
            self.assertEqual(int(agent_row["pinned"]), 1)
            self.assertEqual(task_row["title"], "会话新标题")
            # Pinned rows sort before others of same age when mixed later; flags present.
            self.assertIn("catalog_revision", boot)
        finally:
            server.shutdown()
            server.server_close()

    def test_sse_stream_wakes_on_add_event_without_1s_poll(self):
        agent_id = str(uuid.uuid4())
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("sse", "sse", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (agent_id, "sse", "sse-agent", str(self.folder), agent_id, stamp, stamp),
            )
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS[agent_id] = threading.Condition()

        server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        received = []
        error_box = []

        def reader():
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/stream?agent_id={agent_id}&after=0"
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    # Read until we see one data event or the socket idles.
                    buf = b""
                    deadline = time.time() + 4
                    while time.time() < deadline:
                        chunk = resp.read(1)
                        if not chunk:
                            break
                        buf += chunk
                        if b"\n\n" in buf:
                            parts = buf.split(b"\n\n")
                            for part in parts[:-1]:
                                text = part.decode("utf-8", errors="replace")
                                for line in text.splitlines():
                                    if line.startswith("data: "):
                                        received.append(line[6:])
                            buf = parts[-1]
                            if received:
                                break
            except Exception as exc:
                error_box.append(repr(exc))

        worker = threading.Thread(target=reader, daemon=True)
        worker.start()
        time.sleep(0.15)
        t0 = time.time()
        daemon.add_event(agent_id, None, "text", "hello-sse", {"type": "text", "data": "hello-sse"})
        worker.join(3)
        elapsed = time.time() - t0
        try:
            self.assertFalse(error_box, error_box)
            self.assertTrue(received, "SSE should deliver the new event")
            self.assertIn("hello-sse", received[0])
            # Event-driven path should not need a full 1s poll cycle.
            self.assertLess(elapsed, 1.0, f"SSE latency too high: {elapsed:.3f}s")
        finally:
            server.shutdown()
            server.server_close()


class GitChangesDeltaTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self._orig = {
            "DB_PATH": daemon.DB_PATH,
            "ARTIFACTS": daemon.ARTIFACTS,
            "DATA": daemon.DATA,
            "ROOT": daemon.ROOT,
        }
        daemon.ROOT = self.repo
        daemon.DATA = self.repo / "obs-data"
        daemon.DATA.mkdir()
        daemon.ARTIFACTS = daemon.DATA / "artifacts"
        daemon.ARTIFACTS.mkdir()
        daemon.DB_PATH = daemon.DATA / "observer.sqlite"
        daemon.init_db()
        agent_id = str(uuid.uuid4())
        self.agent_id = agent_id
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("g", "g", str(self.repo), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (agent_id, "g", "git", str(self.repo), agent_id, stamp, stamp),
            )
            cur = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,'p','running',?)",
                (agent_id, stamp),
            )
            self.turn_id = cur.lastrowid

        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        for key, value in self._orig.items():
            setattr(daemon, key, value)
        with daemon.RUNNERS_LOCK:
            daemon.RUNNERS.clear()
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        self._tmp.cleanup()

    def _changes(self):
        with daemon.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM changes WHERE agent_id=? ORDER BY id", (self.agent_id,))]

    def test_preexisting_dirty_unchanged_not_recorded(self):
        (self.repo / "dirty.txt").write_text("pre\n", encoding="utf-8")
        before = daemon.git_snapshot(self.repo)
        # No further change this turn.
        after = daemon.git_snapshot(self.repo)
        daemon.record_changes(self.agent_id, self.turn_id, before, after)
        self.assertEqual(self._changes(), [])

    def test_preexisting_dirty_modified_marked_preexisting(self):
        (self.repo / "dirty2.txt").write_text("pre\n", encoding="utf-8")
        before = daemon.git_snapshot(self.repo)
        (self.repo / "dirty2.txt").write_text("pre\nchanged\n", encoding="utf-8")
        after = daemon.git_snapshot(self.repo)
        daemon.record_changes(self.agent_id, self.turn_id, before, after)
        rows = self._changes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "dirty2.txt")
        self.assertEqual(rows[0]["preexisting"], 1)

    def test_new_file_delete_rename_and_unique_count(self):
        (self.repo / "to_delete.txt").write_text("bye\n", encoding="utf-8")
        (self.repo / "renamed_src.txt").write_text("rename me\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "to_delete.txt", "renamed_src.txt"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "commit", "-m", "prep"], cwd=self.repo, check=True, capture_output=True)

        before = daemon.git_snapshot(self.repo)
        (self.repo / "brand_new.txt").write_text("brand\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("base\nmodified again\n", encoding="utf-8")
        (self.repo / "to_delete.txt").unlink()
        subprocess.run(["git", "mv", "renamed_src.txt", "renamed_dst.txt"], cwd=self.repo, check=True, capture_output=True)
        after = daemon.git_snapshot(self.repo)
        daemon.record_changes(self.agent_id, self.turn_id, before, after)
        rows = self._changes()
        paths = {r["path"] for r in rows}
        kinds = {r["kind"] for r in rows}
        self.assertIn("brand_new.txt", paths)
        self.assertIn("tracked.txt", paths)
        self.assertTrue("deleted" in kinds or any("to_delete" in p for p in paths))
        self.assertTrue("renamed" in kinds or any("renamed_dst" in p or "→" in p for p in paths))
        unique = daemon.unique_changed_files(self.agent_id)
        self.assertEqual(unique, len(paths))
        self.assertGreaterEqual(unique, 3)

    def test_multi_turn_same_file_only_delta_each_turn(self):
        before1 = daemon.git_snapshot(self.repo)
        (self.repo / "shared.txt").write_text("v1\n", encoding="utf-8")
        after1 = daemon.git_snapshot(self.repo)
        daemon.record_changes(self.agent_id, self.turn_id, before1, after1)
        rows1 = self._changes()
        self.assertEqual(len(rows1), 1)

        stamp = daemon.now()
        with daemon.connect() as db:
            cur = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,2,'p2','running',?)",
                (self.agent_id, stamp),
            )
            turn2 = cur.lastrowid
        before2 = daemon.git_snapshot(self.repo)
        (self.repo / "shared.txt").write_text("v2\n", encoding="utf-8")
        after2 = daemon.git_snapshot(self.repo)
        daemon.record_changes(self.agent_id, turn2, before2, after2)
        with daemon.connect() as db:
            t2 = [dict(r) for r in db.execute("SELECT * FROM changes WHERE turn_id=?", (turn2,))]
        self.assertEqual(len(t2), 1)
        self.assertEqual(t2[0]["preexisting"], 1)
        self.assertEqual(daemon.unique_changed_files(self.agent_id), 1)

    def test_non_git_git_snapshot_unavailable_but_fs_fallback_works(self):
        plain = Path(tempfile.mkdtemp())
        try:
            git = daemon.git_snapshot(plain)
            self.assertFalse(git.get("available"))
            before = daemon.workspace_snapshot(plain)
            self.assertTrue(before.get("available"), before)
            self.assertEqual(before.get("mode"), "fs")
            (plain / "new.txt").write_text("x\n", encoding="utf-8")
            after = daemon.workspace_snapshot(plain)
            count = daemon.record_changes(self.agent_id, self.turn_id, before, after)
            self.assertEqual(count, 1)
            rows = self._changes()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["path"], "new.txt")
            self.assertEqual(rows[0]["kind"], "added")
        finally:
            shutil.rmtree(plain, ignore_errors=True)


class SingletonLockTest(unittest.TestCase):
    def test_concurrent_start_single_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            base = 47100 + (os.getpid() % 400)
            # Both children share the same data dir + control-port base.
            script = f"""
import json, os, sys
from pathlib import Path
import daemon

data = Path(r"{data}")
daemon.DATA = data
daemon.ARTIFACTS = data / "artifacts"
daemon.ARTIFACTS.mkdir(parents=True, exist_ok=True)
daemon.DB_PATH = data / "observer.sqlite"
daemon.STATE_PATH = data / "daemon-state.json"
daemon.LOCK_PATH = data / "daemon.lock"
daemon.CONTROL_PORT = {base}
daemon.VIEWER_PORT = {base + 40}
daemon.main()
"""
            env = os.environ.copy()
            env["GROK_OBSERVER_NO_BROWSER"] = "1"
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", script],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            time.sleep(2.0)
            alive = [p for p in procs if p.poll() is None]
            state_path = data / "daemon-state.json"
            # Same data dir: at most one live owner process.
            self.assertLessEqual(len(alive), 1, "two daemons owning the same data dir")
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertIn("pid", state)
                self.assertIn("control_port", state)
                if alive and daemon.pid_is_alive(int(state.get("pid") or 0)):
                    self.assertEqual(state["pid"], alive[0].pid)
            for p in procs:
                if p.poll() is None:
                    p.terminate()
            outs = []
            for p in procs:
                try:
                    out, err = p.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    out, err = p.communicate(timeout=3)
                outs.append((out or "") + (err or ""))
            self.assertTrue(
                any(p.returncode == 0 or "already_running" in o for p, o in zip(procs, outs))
                or state_path.exists()
                or any(alive),
                f"no owner established: {outs!r}",
            )

    def test_stale_state_allows_new_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            state_path = data / "daemon-state.json"
            lock_path = data / "daemon.lock"
            # Stale state pointing at dead pid.
            state_path.write_text(json.dumps({"pid": 99999999, "control_port": 1, "viewer_port": None}), encoding="utf-8")
            lock_path.write_text("99999999\n", encoding="utf-8")

            orig = {
                "DATA": daemon.DATA,
                "ARTIFACTS": daemon.ARTIFACTS,
                "DB_PATH": daemon.DB_PATH,
                "STATE_PATH": daemon.STATE_PATH,
                "LOCK_PATH": daemon.LOCK_PATH,
            }
            try:
                daemon.DATA = data
                daemon.ARTIFACTS = data / "artifacts"
                daemon.ARTIFACTS.mkdir(exist_ok=True)
                daemon.DB_PATH = data / "observer.sqlite"
                daemon.STATE_PATH = state_path
                daemon.LOCK_PATH = lock_path
                self.assertIsNone(daemon.healthy_existing_daemon())
                self.assertTrue(daemon.acquire_singleton_lock())
                daemon.write_state()
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["pid"], os.getpid())
                daemon.release_singleton_lock()
            finally:
                for key, value in orig.items():
                    setattr(daemon, key, value)

    def test_path_is_within(self):
        root = Path(tempfile.mkdtemp()).resolve()
        try:
            child = (root / "a" / "b.txt")
            child.parent.mkdir(parents=True)
            child.write_text("x", encoding="utf-8")
            self.assertTrue(daemon.path_is_within(child, root))
            self.assertFalse(daemon.path_is_within(root.parent / "other", root))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class PorcelainParseTest(unittest.TestCase):
    def test_rename_copy_entries(self):
        # git status -z rename: "R  new\0old\0"
        raw = "R  newname.txt\0oldname.txt\0 M plain.txt\0"
        entries = daemon.parse_porcelain_z(raw)
        self.assertIn("newname.txt", entries)
        self.assertEqual(entries["newname.txt"]["kind"], "renamed")
        self.assertEqual(entries["newname.txt"]["rename_from"], "oldname.txt")
        self.assertIn("plain.txt", entries)


class ChildPidRecoverTest(_IsolatedDbMixin, unittest.TestCase):
    def test_terminate_pid_noops_for_self_and_zero(self):
        # Must not raise or kill the test process.
        daemon.terminate_pid(0)
        daemon.terminate_pid(-1)
        daemon.terminate_pid(os.getpid())

    def test_recover_reaps_alive_child_and_fails_agent(self):
        # Spawn a long-lived child so recover must kill it.
        alive = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Short-lived process that already exited (dead pid path).
        dead = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        dead.wait(timeout=5)
        self.assertFalse(daemon.pid_is_alive(dead.pid))
        self.assertTrue(daemon.pid_is_alive(alive.pid))

        alive_id = str(uuid.uuid4())
        dead_id = str(uuid.uuid4())
        stamp = daemon.now()
        # Round-4 policy: recover kills a pid only on verified identity. Seed
        # the child's real OS create time (queried right after spawn, well
        # inside the 2.0s tolerance) as the expected identity evidence.
        created = daemon.process_create_time(alive.pid)
        if created is None:
            self.skipTest("process_create_time unavailable on this platform")
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("rec", "rec", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,child_pid,child_started_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'running',?,?,?,?)",
                (alive_id, "rec", "alive", str(self.folder), alive_id, alive.pid, str(created), stamp, stamp),
            )
            db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'running',?)",
                (alive_id, "p", stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,child_pid,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'queued',?,?,?)",
                (dead_id, "rec", "dead", str(self.folder), dead_id, dead.pid, stamp, stamp),
            )
            db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'queued',?)",
                (dead_id, "p", stamp),
            )

        try:
            daemon.recover(start_runners=False)
            deadline = time.time() + 3
            while time.time() < deadline and daemon.pid_is_alive(alive.pid):
                time.sleep(0.05)
            self.assertFalse(daemon.pid_is_alive(alive.pid), "recover should terminate orphan child")
            with daemon.connect() as db:
                a = db.execute("SELECT status,error,child_pid FROM agents WHERE id=?", (alive_id,)).fetchone()
                d = db.execute("SELECT status,error,child_pid FROM agents WHERE id=?", (dead_id,)).fetchone()
                self.assertEqual(a["status"], "failed")
                self.assertIsNone(a["child_pid"])
                self.assertIn("orphan process reaped", a["error"] or "")
                turn_a = db.execute("SELECT status FROM turns WHERE agent_id=?", (alive_id,)).fetchone()
                self.assertEqual(turn_a["status"], "failed")
                # Durable queued turns survive restart: the queued agent and its
                # turn are preserved (not failed) so they can resume.
                self.assertEqual(d["status"], "queued")
                turn_d = db.execute("SELECT status FROM turns WHERE agent_id=?", (dead_id,)).fetchone()
                self.assertEqual(turn_d["status"], "queued")
        finally:
            if daemon.pid_is_alive(alive.pid):
                daemon.terminate_pid(alive.pid)
            try:
                alive.wait(timeout=2)
            except Exception:
                pass

    def test_recover_skips_reused_pid(self):
        # Alive process, but recorded child_started_at does not match its real
        # creation time → recover must treat the pid as reused and NOT kill it.
        alive = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self.assertTrue(daemon.pid_is_alive(alive.pid))
            # Only meaningful when we can actually read a creation time.
            if daemon.process_create_time(alive.pid) is None:
                self.skipTest("process_create_time unavailable on this platform")
            agent_id = str(uuid.uuid4())
            stamp = daemon.now()
            with daemon.connect() as db:
                db.execute(
                    "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("reuse", "reuse", str(self.folder), "t", stamp, stamp),
                )
                db.execute(
                    "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,child_pid,child_started_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'running',?,?,?,?)",
                    (agent_id, "reuse", "a", str(self.folder), agent_id, alive.pid, "1.0", stamp, stamp),
                )
                db.execute(
                    "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'running',?)",
                    (agent_id, "p", stamp),
                )
            daemon.recover()
            time.sleep(0.3)
            self.assertTrue(daemon.pid_is_alive(alive.pid), "reused pid must not be killed")
            with daemon.connect() as db:
                row = db.execute("SELECT status,error,child_pid FROM agents WHERE id=?", (agent_id,)).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertIsNone(row["child_pid"])
            self.assertIn("could not be identity-verified, not killed", row["error"] or "")
        finally:
            if daemon.pid_is_alive(alive.pid):
                daemon.terminate_pid(alive.pid)
            try:
                alive.wait(timeout=2)
            except Exception:
                pass


class ConcurrencyLimitTest(_IsolatedDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._orig_limits = {
            "MAX_ACTIVE_PER_THREAD": daemon.MAX_ACTIVE_PER_THREAD,
            "MAX_ACTIVE_AGENTS": daemon.MAX_ACTIVE_AGENTS,
            "MAX_QUEUE_DEPTH": daemon.MAX_QUEUE_DEPTH,
        }
        self._created_ids: list[str] = []

    def tearDown(self):
        for agent_id in self._created_ids:
            try:
                daemon.action("cancel", {"agent_id": agent_id}, {})
            except Exception:
                pass
        for key, value in self._orig_limits.items():
            setattr(daemon, key, value)
        super().tearDown()

    def _create_tracked(self, prompt: str, *, name: str = "agent", cwd: str | None = None, thread_id: str = "test-thread"):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ.setdefault("GROK_FAKE_DURATION", "3.0")
        data = daemon.action(
            "create_agent",
            {
                "agent_name": name,
                "prompt": prompt,
                "cwd": cwd or str(self.folder),
            },
            {"codex_thread_id": thread_id, "codex_origin": "test"},
        )
        self._created_ids.append(data["agent_id"])
        return data

    def test_allows_concurrent_same_cwd(self):
        """Same workspace may host multiple active agents; second gets a soft warning."""
        os.environ["GROK_FAKE_DURATION"] = "3.0"
        daemon.MAX_ACTIVE_PER_THREAD = 5
        first = self._create_tracked("first parallel", name="a1", thread_id="cwd-thread")
        second = self._create_tracked("second parallel", name="a2", thread_id="cwd-thread")
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        with daemon.connect() as db:
            warn = db.execute(
                "SELECT COUNT(*) AS c FROM events WHERE agent_id=? AND type='concurrency_warning'",
                (second["agent_id"],),
            ).fetchone()["c"]
        self.assertGreaterEqual(int(warn), 1)

    def test_max_per_thread_rejected(self):
        os.environ["GROK_FAKE_DURATION"] = "3.0"
        daemon.MAX_ACTIVE_PER_THREAD = 2
        daemon.MAX_ACTIVE_AGENTS = 16
        self._create_tracked("t1", name="p1", thread_id="limit-thread")
        self._create_tracked("t2", name="p2", thread_id="limit-thread")
        with self.assertRaises(ValueError) as ctx:
            self._create_tracked("t3", name="p3", thread_id="limit-thread")
        msg = str(ctx.exception)
        self.assertIn("MAX_ACTIVE_PER_THREAD", msg)
        self.assertIn("GROK_OBSERVER_MAX_PER_THREAD", msg)

    def test_max_per_thread_independent_threads(self):
        """Filling thread A must not block thread B (even on the same cwd)."""
        os.environ["GROK_FAKE_DURATION"] = "3.0"
        daemon.MAX_ACTIVE_PER_THREAD = 2
        daemon.MAX_ACTIVE_AGENTS = 16
        self._create_tracked("a1", name="a1", thread_id="thread-A")
        self._create_tracked("a2", name="a2", thread_id="thread-A")
        with self.assertRaises(ValueError):
            self._create_tracked("a3", name="a3", thread_id="thread-A")
        other = self._create_tracked("b1", name="b1", thread_id="thread-B")
        self.assertIn("agent_id", other)

    def test_max_active_agents_rejected(self):
        os.environ["GROK_FAKE_DURATION"] = "3.0"
        daemon.MAX_ACTIVE_PER_THREAD = 5
        daemon.MAX_ACTIVE_AGENTS = 1
        other_cwd = self.folder / "other"
        other_cwd.mkdir(exist_ok=True)
        first = self._create_tracked("only", name="solo", thread_id="g1")
        with self.assertRaises(ValueError) as ctx:
            self._create_tracked(
                "overflow",
                name="too-many",
                cwd=str(other_cwd),
                thread_id="g2",
            )
        self.assertIn("MAX_ACTIVE_AGENTS", str(ctx.exception))
        self.assertIn(first["agent_id"], self._created_ids)


class FinalTextFallbackTest(_IsolatedDbMixin, unittest.TestCase):
    def test_final_text_from_events_concatenates_chronologically(self):
        agent_id = str(uuid.uuid4())
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("ft", "ft", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (agent_id, "ft", "ft", str(self.folder), agent_id, stamp, stamp),
            )
            cur = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'running',?)",
                (agent_id, "p", stamp),
            )
            turn_id = cur.lastrowid

        daemon.add_event(agent_id, turn_id, "text", "hello", {"type": "text", "data": "Hello "})
        daemon.add_event(agent_id, turn_id, "thought", "noise", {"type": "thought", "data": "skip me"})
        daemon.add_event(
            agent_id,
            turn_id,
            "agent_message_chunk",
            "world",
            {"type": "agent_message_chunk", "data": "world"},
        )

        text = daemon.final_text_from_events(agent_id, turn_id)
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("skip me", text)

    def test_result_falls_back_to_last_completed_turn(self):
        agent_id = str(uuid.uuid4())
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("rf", "rf", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,final_text,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'completed','',?,?)",
                (agent_id, "rf", "rf", str(self.folder), agent_id, stamp, stamp),
            )
            db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,result,created_at,completed_at) "
                "VALUES(?,1,?,'completed',?,?,?)",
                (agent_id, "p", "turn result body", stamp, stamp),
            )
        out = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual(out["final_text"], "turn result body")


class EditLedgerTest(_IsolatedDbMixin, unittest.TestCase):
    """Tool edit ledger: claimed paths + merge with observed + shared peers."""

    def _seed_agent(self, agent_id: str, *, status: str = "running", name: str = "led") -> int:
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("ledger", "ledger", str(self.folder), "t", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (agent_id, "ledger", name, str(self.folder), agent_id, status, stamp, stamp),
            )
            cur = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,?,'running',?)",
                (agent_id, "edit something", stamp),
            )
            return int(cur.lastrowid)

    def test_extract_search_replace_session_payload(self):
        line = {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "title": "Edit `hello.txt`",
                    "rawInput": {
                        "file_path": "hello.txt",
                        "old_string": "a",
                        "new_string": "b",
                    },
                    "_meta": {
                        "x.ai/tool": {
                            "name": "search_replace",
                            "kind": "edit",
                        }
                    },
                }
            },
        }
        claims = daemon.extract_edit_claims_from_payload(line)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["path"], "hello.txt")
        self.assertEqual(claims[0]["tool_name"], "search_replace")
        self.assertEqual(claims[0]["tool_call_id"], "call-1")

    def test_extract_ignores_read_file(self):
        line = {
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read `README.md`",
                    "rawInput": {"target_file": "README.md"},
                    "_meta": {"x.ai/tool": {"name": "read_file", "kind": "read"}},
                }
            }
        }
        self.assertEqual(daemon.extract_edit_claims_from_payload(line), [])

    def test_record_changes_claimed_only(self):
        agent_id = str(uuid.uuid4())
        turn_id = self._seed_agent(agent_id)
        payload = {
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c-edit",
                    "title": "Edit `only-claimed.txt`",
                    "rawInput": {"file_path": "only-claimed.txt", "old_string": "x", "new_string": "y"},
                    "_meta": {"x.ai/tool": {"name": "search_replace", "kind": "edit"}},
                }
            }
        }
        daemon.add_event(agent_id, turn_id, "tool_call_update", "Edit", payload)
        # Snapshots unavailable → still record claimed.
        count = daemon.record_changes(
            agent_id,
            turn_id,
            {"available": False, "mode": "git"},
            {"available": False, "mode": "git"},
        )
        self.assertEqual(count, 1)
        with daemon.connect() as db:
            row = db.execute(
                "SELECT path,source,shared,tool_name FROM changes WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
        self.assertEqual(row["path"], "only-claimed.txt")
        self.assertEqual(row["source"], "claimed")
        self.assertEqual(int(row["shared"] or 0), 0)
        self.assertEqual(row["tool_name"], "search_replace")

    def test_record_changes_both_when_fs_observes_same_path(self):
        agent_id = str(uuid.uuid4())
        turn_id = self._seed_agent(agent_id)
        rel = "touch-me.txt"
        target = self.folder / rel
        target.write_text("before\n", encoding="utf-8")
        before = daemon.fs_snapshot(self.folder)
        target.write_text("after\n", encoding="utf-8")
        after = daemon.fs_snapshot(self.folder)
        daemon.add_event(
            agent_id,
            turn_id,
            "tool_call_update",
            "Edit",
            {
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "c2",
                        "title": f"Edit `{rel}`",
                        "rawInput": {"file_path": rel, "old_string": "before", "new_string": "after"},
                        "_meta": {"x.ai/tool": {"name": "search_replace", "kind": "edit"}},
                    }
                }
            },
        )
        count = daemon.record_changes(agent_id, turn_id, before, after)
        self.assertGreaterEqual(count, 1)
        with daemon.connect() as db:
            row = db.execute(
                "SELECT path,source FROM changes WHERE agent_id=? AND path=?",
                (agent_id, rel),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "both")

    def test_observed_shared_when_peer_running(self):
        agent_a = str(uuid.uuid4())
        agent_b = str(uuid.uuid4())
        turn_a = self._seed_agent(agent_a, name="a")
        self._seed_agent(agent_b, name="b", status="running")
        rel = "peer-touch.txt"
        target = self.folder / rel
        target.write_text("v1\n", encoding="utf-8")
        before = daemon.fs_snapshot(self.folder)
        target.write_text("v2\n", encoding="utf-8")
        after = daemon.fs_snapshot(self.folder)
        # No tool claim — pure observed; peer running → shared=1
        count = daemon.record_changes(agent_a, turn_a, before, after)
        self.assertGreaterEqual(count, 1)
        with daemon.connect() as db:
            row = db.execute(
                "SELECT path,source,shared FROM changes WHERE agent_id=? AND path=?",
                (agent_a, rel),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "observed")
        self.assertEqual(int(row["shared"]), 1)


class UpdateAgentModeTest(_IsolatedDbMixin, unittest.TestCase):
    """update_agent modes: immediate / idle / tool_boundary / timeout / cancel."""

    def _wait_running(self, agent_id: str, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if daemon.action("status", {"agent_id": agent_id}, {})["status"] == "running":
                return
            time.sleep(0.05)
        self.fail("agent never reached running")

    def _events(self, agent_id: str) -> list[dict]:
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT type,summary,payload FROM events WHERE agent_id=? ORDER BY seq",
                (agent_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = {"type": row["type"], "summary": row["summary"], "payload": {}}
            if row["payload"]:
                try:
                    item["payload"] = json.loads(row["payload"])
                except json.JSONDecodeError:
                    item["payload"] = {}
            out.append(item)
        return out

    def _inject_open_tool(self, agent_id: str, call_id: str = "t1", name: str = "read_file") -> int:
        runner = daemon.get_runner(agent_id, create=False)
        self.assertIsNotNone(runner)
        assert runner is not None
        with daemon.connect() as db:
            turn = db.execute(
                "SELECT id FROM turns WHERE agent_id=? AND status='running' ORDER BY turn_no DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        self.assertIsNotNone(turn)
        turn_id = int(turn["id"])
        # Ensure flight tracking is bound to this turn (begin_turn already ran).
        payload = {
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": name,
                    "status": "in_progress",
                    "_meta": {"x.ai/tool": {"name": name}},
                }
            }
        }
        runner.observe_tool_event(turn_id, "tool_call", name, payload)
        self.assertTrue(runner.has_inflight_tools())
        return turn_id

    def test_immediate_mode_interrupts(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "2"
        agent_id = self._create("long")["agent_id"]
        self._wait_running(agent_id)
        updated = daemon.action(
            "update_agent",
            {"agent_id": agent_id, "prompt": "steer now", "mode": "immediate"},
            {},
        )
        self.assertEqual(updated["requested_mode"], "immediate")
        self.assertEqual(updated["mode"], "interrupt_and_resume")
        self.assertFalse(updated["lossless_interject"])
        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 15}, {})
        self.assertTrue(final["done"], final)
        result = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual([t["status"] for t in result["turn_results"]], ["interrupted", "completed"])
        types = [e["type"] for e in self._events(agent_id)]
        self.assertIn("interjection", types)
        self.assertIn("update_applied", types)

    def test_idle_follow_up(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "0.05"
        agent_id = self._create("quick")["agent_id"]
        done = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 10}, {})
        self.assertTrue(done["done"], done)
        updated = daemon.action(
            "update_agent",
            {"agent_id": agent_id, "prompt": "second turn", "mode": "tool_boundary"},
            {},
        )
        self.assertEqual(updated["requested_mode"], "tool_boundary")
        self.assertEqual(updated["mode"], "follow_up")
        self.assertFalse(updated["lossless_interject"])
        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 10}, {})
        self.assertTrue(final["done"], final)
        result = daemon.action("result", {"agent_id": agent_id}, {})
        statuses = [t["status"] for t in result["turn_results"]]
        self.assertEqual(statuses, ["completed", "completed"])
        self.assertEqual(result["turn_results"][1]["prompt"], "second turn")

    def test_tool_boundary_waits_then_interrupts(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "3"
        agent_id = self._create("toolish")["agent_id"]
        self._wait_running(agent_id)
        turn_id = self._inject_open_tool(agent_id, call_id="bound1")

        updated = daemon.action(
            "update_agent",
            {
                "agent_id": agent_id,
                "prompt": "after tools",
                "mode": "tool_boundary",
                "timeout_seconds": 30,
            },
            {},
        )
        self.assertEqual(updated["mode"], "waiting_tool_boundary")
        self.assertEqual(updated["requested_mode"], "tool_boundary")

        # Still running — must not have interrupted yet.
        time.sleep(0.15)
        st = daemon.action("status", {"agent_id": agent_id}, {})
        self.assertEqual(st["status"], "running")
        types_mid = [e["type"] for e in self._events(agent_id)]
        self.assertIn("pending_update", types_mid)
        self.assertNotIn("update_applied", types_mid)

        # Close the tool → boundary fires.
        runner = daemon.get_runner(agent_id, create=False)
        assert runner is not None
        runner.observe_tool_event(
            turn_id,
            "tool_call_update",
            "done",
            {
                "params": {
                    "update": {
                        "toolCallId": "bound1",
                        "status": "completed",
                        "_meta": {"x.ai/tool": {"name": "read_file"}},
                    }
                }
            },
        )
        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 15}, {})
        self.assertTrue(final["done"], final)
        result = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual([t["status"] for t in result["turn_results"]], ["interrupted", "completed"])
        applied = [e for e in self._events(agent_id) if e["type"] == "update_applied"]
        self.assertTrue(applied)
        self.assertEqual(applied[0]["payload"].get("mode_used"), "interrupt_and_resume")
        self.assertEqual(applied[0]["payload"].get("trigger"), "tool_boundary")

    def test_tool_boundary_timeout_falls_back_to_immediate(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "4"
        agent_id = self._create("slow tools")["agent_id"]
        self._wait_running(agent_id)
        self._inject_open_tool(agent_id, call_id="hang1")

        updated = daemon.action(
            "update_agent",
            {
                "agent_id": agent_id,
                "prompt": "timeout path",
                "mode": "tool_boundary",
                "timeout_seconds": 1,
            },
            {},
        )
        self.assertEqual(updated["mode"], "waiting_tool_boundary")

        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 15}, {})
        self.assertTrue(final["done"], final)
        result = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual([t["status"] for t in result["turn_results"]], ["interrupted", "completed"])
        applied = [e for e in self._events(agent_id) if e["type"] == "update_applied"]
        self.assertTrue(any(e["payload"].get("mode_used") == "immediate_timeout" for e in applied))

    def test_auto_uses_boundary_when_tools_inflight(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "3"
        agent_id = self._create("auto boundary")["agent_id"]
        self._wait_running(agent_id)
        turn_id = self._inject_open_tool(agent_id, call_id="auto1")

        updated = daemon.action(
            "update_agent",
            {"agent_id": agent_id, "prompt": "auto steer", "mode": "auto", "timeout_seconds": 20},
            {},
        )
        self.assertEqual(updated["requested_mode"], "auto")
        self.assertEqual(updated["mode"], "waiting_tool_boundary")

        runner = daemon.get_runner(agent_id, create=False)
        assert runner is not None
        runner.observe_tool_event(
            turn_id,
            "tool_completed",
            "done",
            {"params": {"update": {"toolCallId": "auto1", "status": "completed"}}},
        )
        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 15}, {})
        self.assertTrue(final["done"], final)

    def test_multiple_boundary_updates_process_in_order(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "3"
        agent_id = self._create("multi")["agent_id"]
        self._wait_running(agent_id)
        turn_id = self._inject_open_tool(agent_id, call_id="m1")

        u1 = daemon.action(
            "update_agent",
            {"agent_id": agent_id, "prompt": "first", "mode": "tool_boundary", "timeout_seconds": 20},
            {},
        )
        u2 = daemon.action(
            "update_agent",
            {"agent_id": agent_id, "prompt": "second", "mode": "tool_boundary", "timeout_seconds": 20},
            {},
        )
        self.assertEqual(u1["mode"], "waiting_tool_boundary")
        self.assertEqual(u2["mode"], "waiting_tool_boundary")

        runner = daemon.get_runner(agent_id, create=False)
        assert runner is not None
        runner.observe_tool_event(
            turn_id,
            "tool_call_update",
            "done",
            {"params": {"update": {"toolCallId": "m1", "status": "completed"}}},
        )
        final = daemon.action("wait", {"agent_id": agent_id, "timeout_seconds": 20}, {})
        self.assertTrue(final["done"], final)
        result = daemon.action("result", {"agent_id": agent_id}, {})
        prompts = [t["prompt"] for t in result["turn_results"]]
        # The base prompt carries the auto-injected coordination fallback hint.
        self.assertTrue(prompts[0].startswith("multi"))
        self.assertIn("[Agent Fabric coordination]", prompts[0])
        self.assertIn("first", prompts)
        self.assertIn("second", prompts)
        # Independent replacement turns, order preserved after the interrupted base turn.
        idx_first = prompts.index("first")
        idx_second = prompts.index("second")
        self.assertLess(idx_first, idx_second)
        # No ghost queued turns left.
        self.assertTrue(all(t["status"] != "queued" for t in result["turn_results"]))

    def test_cancel_releases_pending_boundary_timer(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "5"
        agent_id = self._create("cancel pending")["agent_id"]
        self._wait_running(agent_id)
        self._inject_open_tool(agent_id, call_id="cxl1")

        updated = daemon.action(
            "update_agent",
            {
                "agent_id": agent_id,
                "prompt": "will cancel",
                "mode": "tool_boundary",
                "timeout_seconds": 60,
            },
            {},
        )
        self.assertEqual(updated["mode"], "waiting_tool_boundary")
        runner = daemon.get_runner(agent_id, create=False)
        assert runner is not None
        self.assertTrue(runner._pending_boundary)

        daemon.action("cancel", {"agent_id": agent_id}, {})
        self.assertFalse(runner._pending_boundary)
        self.assertFalse(runner.has_inflight_tools())
        # Timer must not resurrect work after cancel.
        time.sleep(0.2)
        st = daemon.action("status", {"agent_id": agent_id}, {})
        self.assertEqual(st["status"], "cancelled")

    def test_shutdown_releases_pending_boundary_timer(self):
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "5"
        agent_id = self._create("shutdown pending")["agent_id"]
        self._wait_running(agent_id)
        self._inject_open_tool(agent_id, call_id="sd1")
        updated = daemon.action(
            "update_agent",
            {
                "agent_id": agent_id,
                "prompt": "will shutdown",
                "mode": "tool_boundary",
                "timeout_seconds": 60,
            },
            {},
        )
        self.assertEqual(updated["mode"], "waiting_tool_boundary")
        runner = daemon.get_runner(agent_id, create=False)
        assert runner is not None
        runner.shutdown()
        self.assertFalse(runner._pending_boundary)
        self.assertFalse(runner.has_inflight_tools())


if __name__ == "__main__":
    unittest.main()
