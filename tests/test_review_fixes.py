"""Regression tests for the coordination runtime hardening review fixes (P1-1..P1-8).

Covers the worker trust boundary (token authentication, host/worker surface
split, worker control port env), hub_token redaction in viewer payloads,
durable DB-backed delivery scheduling (completed-worker wake, crash recovery,
queue-full survival, prompt envelope cap), wait_any thread isolation, wait
timeouts, and retention/FK lifecycle (tasks survive while mailbox history
exists).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import daemon
import grok_hub
import server as mcp_server
from coordination import Mailbox, main_peer_id


ROOT = Path(__file__).resolve().parents[1]


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
        # Never attempt to launch Grok or a browser from these tests.
        self._prev_no_browser = os.environ.get("GROK_OBSERVER_NO_BROWSER")
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
        if self._prev_no_browser is None:
            os.environ.pop("GROK_OBSERVER_NO_BROWSER", None)
        else:
            os.environ["GROK_OBSERVER_NO_BROWSER"] = self._prev_no_browser
        self._tmp.cleanup()

    def _seed_task(self, thread_id: str) -> None:
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (thread_id, thread_id, ".", "test", stamp, stamp),
            )

    def _seed_worker(
        self,
        thread_id: str,
        name: str = "a",
        status: str = "queued",
        token: str | None = None,
    ) -> tuple[str, str]:
        """Insert a worker row directly; returns (agent_id, hub_token).

        Never spawns Grok. When token is None a deterministic test token is
        generated ('tok-' + uuid4 hex) and stored in the row.
        """
        agent_id = str(uuid.uuid4())
        if token is None:
            token = "tok-" + uuid.uuid4().hex
        self._seed_task(thread_id)
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,display_title,"
                "hub_token,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (agent_id, thread_id, name, ".", agent_id, status, name, token, stamp, stamp),
            )
        return agent_id, token

    def _seed_turn(
        self,
        agent_id: str,
        turn_no: int = 1,
        status: str = "completed",
        prompt: str = "seed turn",
    ) -> int:
        stamp = daemon.now()
        with daemon.connect() as db:
            cursor = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?)",
                (agent_id, turn_no, prompt, status, stamp, stamp if status != "running" else None),
            )
            return int(cursor.lastrowid)

    def _seed_message(
        self,
        thread_id: str,
        to_peer: str,
        body: str,
        from_peer: str | None = None,
        created_at: str | None = None,
        message_id: str | None = None,
        target_turn_id: int | None = None,
    ) -> str:
        """Insert one agent_message directly; returns its id."""
        if from_peer is None:
            from_peer = main_peer_id(thread_id)
        if created_at is None:
            created_at = daemon.now()
        if message_id is None:
            message_id = str(uuid.uuid4())
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,body,created_at,"
                "target_turn_id) VALUES(?,?,?,?,?,?,?)",
                (message_id, thread_id, from_peer, to_peer, body, created_at, target_turn_id),
            )
        return message_id

    def _turn_count(self, agent_id: str) -> int:
        with daemon.connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (agent_id,)
                ).fetchone()["c"]
            )

    def _hub_token(self, agent_id: str) -> str | None:
        with daemon.connect() as db:
            row = db.execute("SELECT hub_token FROM agents WHERE id=?", (agent_id,)).fetchone()
        return row["hub_token"] if row else None


class _ViewerHttpMixin:
    """Serve daemon.ViewerHandler on an ephemeral loopback port."""

    def _start_viewer(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _stop_viewer(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self._port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, json.loads(body) if body else {}

    def _post(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}", method="POST", data=b""
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, json.loads(body) if body else {}


class ViewerRedactionTest(_IsolatedDbMixin, _ViewerHttpMixin, unittest.TestCase):
    """P1-2: hub_token (and child pid fields) never reach viewer payloads."""

    def setUp(self):
        super().setUp()
        self._start_viewer()

    def tearDown(self):
        self._stop_viewer()
        super().tearDown()

    def test_viewer_agent_detail_never_exposes_hub_token(self):
        agent_id, token = self._seed_worker("A", status="completed")
        with daemon.connect() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()

        # The public projection drops the secret and pid fields entirely.
        public = daemon.public_agent_dict(row)
        self.assertEqual(public["id"], agent_id)
        self.assertNotIn("hub_token", public)
        self.assertNotIn("child_pid", public)
        self.assertNotIn("child_started_at", public)

        # The real HTTP endpoint must agree: no hub_token in the JSON body.
        status, body = self._get(f"/api/agents/{agent_id}")
        self.assertEqual(status, 200)
        agent = body["agent"]
        self.assertEqual(agent["id"], agent_id)
        self.assertNotIn("hub_token", agent)
        self.assertNotIn("child_pid", agent)
        self.assertNotIn("child_started_at", agent)
        self.assertNotIn(token, json.dumps(body))


class WorkerSurfaceTest(_IsolatedDbMixin, unittest.TestCase):
    """P1-1: the worker surface only knows hub ops, authenticated by token."""

    def test_worker_surface_rejects_host_actions(self):
        worker_id, token = self._seed_worker("A", status="completed")
        with self.assertRaisesRegex(ValueError, "op must be one of"):
            daemon.worker_hub_request(worker_id, token, {"op": "cancel", "agent_id": worker_id})
        # No state change: the agent row is untouched.
        with daemon.connect() as db:
            row = db.execute("SELECT status FROM agents WHERE id=?", (worker_id,)).fetchone()
        self.assertEqual(row["status"], "completed")

    def test_worker_token_cannot_impersonate_other_agent(self):
        a_id, a_token = self._seed_worker("A", name="a", status="completed")
        b_id, _ = self._seed_worker("B", name="b", status="completed")
        with self.assertRaisesRegex(ValueError, "worker authentication failed"):
            daemon.worker_hub_request(b_id, a_token, {"op": "list"})
        with daemon.connect() as db:
            row = db.execute("SELECT id FROM agents WHERE id=?", (a_id,)).fetchone()
        self.assertIsNotNone(row)

    def test_worker_cannot_supply_thread_id(self):
        worker_id, token = self._seed_worker("A", status="completed")
        daemon.worker_hub_request(
            worker_id,
            token,
            {"op": "send", "to": "main:A", "message": "hi", "thread_id": "EVIL-THREAD"},
        )
        # The hub derives the thread from the authenticated identity; a
        # caller-supplied thread_id is ignored.
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT thread_id, to_peer, body FROM agent_messages"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["thread_id"], "A")
        self.assertEqual(rows[0]["to_peer"], "main:A")
        self.assertEqual(rows[0]["body"], "hi")


class DeliverySchedulingTest(_IsolatedDbMixin, unittest.TestCase):
    """P1-3/P1-4/P1-5: durable DB-backed delivery to completed workers."""

    def test_send_to_completed_worker_schedules_followup(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        # The singleton mailbox's commit hook schedules the delivery.
        with mock.patch.object(daemon, "get_runner", return_value=None):
            daemon.MAILBOX.send(
                thread_id="A",
                from_peer="main:A",
                to_peer=agent_id,
                body="follow up",
            )

        with daemon.connect() as db:
            turns = db.execute(
                "SELECT id,turn_no,status,prompt FROM turns WHERE agent_id=? ORDER BY turn_no",
                (agent_id,),
            ).fetchall()
            agent = db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
            messages = db.execute(
                "SELECT state,target_turn_id FROM agent_messages WHERE to_peer=?",
                (agent_id,),
            ).fetchall()

        self.assertEqual(len(turns), 2)
        new_turn = turns[1]
        self.assertEqual(new_turn["turn_no"], 2)
        self.assertEqual(new_turn["status"], "queued")
        self.assertIn("收到 1 条协作消息", new_turn["prompt"])
        self.assertEqual(agent["status"], "queued")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["state"], "pending")
        self.assertEqual(messages[0]["target_turn_id"], new_turn["id"])

    def test_send_to_running_worker_does_not_interrupt(self):
        agent_id, _ = self._seed_worker("A", status="running")
        self._seed_turn(agent_id, turn_no=1, status="running")
        daemon.MAILBOX.send(thread_id="A", from_peer="main:A", to_peer=agent_id, body="ping")

        self.assertEqual(self._turn_count(agent_id), 1)
        with daemon.connect() as db:
            row = db.execute(
                "SELECT state,target_turn_id FROM agent_messages WHERE to_peer=?", (agent_id,)
            ).fetchone()
            status = db.execute(
                "SELECT status FROM agents WHERE id=?", (agent_id,)
            ).fetchone()["status"]
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["target_turn_id"])
        self.assertEqual(status, "running")

    def test_terminal_send_race_no_stuck_message(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        for i in range(5):
            self._seed_message("A", to_peer=agent_id, body=f"race-{i}")

        barrier = threading.Barrier(2)
        results: list[int] = []
        lock = threading.Lock()

        def _schedule() -> None:
            barrier.wait(timeout=10)
            with lock:
                results.append(daemon.maybe_schedule_delivery(agent_id))

        with mock.patch.object(daemon, "get_runner", return_value=None):
            threads = [threading.Thread(target=_schedule) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads))
        # Exactly one scheduler wins; the other claims nothing.
        self.assertEqual(sorted(results), [0, 5])
        self.assertEqual(self._turn_count(agent_id), 2)
        with daemon.connect() as db:
            new_turn = db.execute(
                "SELECT id FROM turns WHERE agent_id=? AND turn_no=2", (agent_id,)
            ).fetchone()
            rows = db.execute(
                "SELECT state,target_turn_id FROM agent_messages WHERE to_peer=?",
                (agent_id,),
            ).fetchall()
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["target_turn_id"], new_turn["id"])

    def test_delivery_crash_after_schedule_recovers(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        turn_id = self._seed_turn(agent_id, turn_no=2, status="queued")
        self._seed_message("A", to_peer=agent_id, body="m1", target_turn_id=turn_id)
        self._seed_message("A", to_peer=agent_id, body="m2", target_turn_id=turn_id)

        # Recovery must not fail the completed agent or unlink its queued
        # delivery turn; the runner wake is a hint only, so stub it out.
        with mock.patch.object(daemon, "get_runner", return_value=None):
            daemon.recover()

        with daemon.connect() as db:
            turn = db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
            rows = db.execute(
                "SELECT state,target_turn_id FROM agent_messages WHERE to_peer=?",
                (agent_id,),
            ).fetchall()
        self.assertEqual(turn["status"], "queued")
        for row in rows:
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["target_turn_id"], turn_id)

        # Once the runner actually starts the turn, linked messages deliver.
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        marked = mailbox.mark_delivered_for_turn(turn_id=turn_id)
        self.assertEqual(marked, 2)
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT state,delivered_at FROM agent_messages WHERE to_peer=?",
                (agent_id,),
            ).fetchall()
        for row in rows:
            self.assertEqual(row["state"], "delivered")
            self.assertIsNotNone(row["delivered_at"])

    def test_queue_full_does_not_lose_message(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        self._seed_message("A", to_peer=agent_id, body="important")

        class _FullRunner:
            def enqueue(self, turn_id: int, prompt: str) -> None:
                raise ValueError("queue full")

        with mock.patch.object(daemon, "get_runner", return_value=_FullRunner()):
            delivered = daemon.maybe_schedule_delivery(agent_id)
        self.assertEqual(delivered, 1)
        self.assertEqual(self._turn_count(agent_id), 2)
        with daemon.connect() as db:
            turn = db.execute(
                "SELECT id,status FROM turns WHERE agent_id=? AND turn_no=2", (agent_id,)
            ).fetchone()
            row = db.execute(
                "SELECT state,target_turn_id FROM agent_messages WHERE to_peer=?", (agent_id,)
            ).fetchone()
        self.assertEqual(turn["status"], "queued")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["target_turn_id"], turn["id"])

        # Messages are claimed, so a later sweep must not create a duplicate.
        with mock.patch.object(daemon, "get_runner", return_value=None):
            again = daemon.maybe_schedule_delivery(agent_id)
        self.assertEqual(again, 0)
        self.assertEqual(self._turn_count(agent_id), 2)

    def test_prompt_cap_leaves_overflow_pending(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        # ~5 KiB each (multi-byte chars keep the char count under the 4000-char
        # per-body cap); 30 of them far exceed the 60 KiB prompt envelope.
        bodies = ["协" * 1666 for _ in range(30)]
        for body in bodies:
            self._seed_message("A", to_peer=agent_id, body=body)

        with mock.patch.object(daemon, "get_runner", return_value=None):
            delivered = daemon.maybe_schedule_delivery(agent_id)

        self.assertGreater(delivered, 0)
        self.assertLess(delivered, 30)
        self.assertEqual(self._turn_count(agent_id), 2)
        with daemon.connect() as db:
            turn = db.execute(
                "SELECT id,prompt FROM turns WHERE agent_id=? AND turn_no=2", (agent_id,)
            ).fetchone()
            rows = db.execute(
                "SELECT state,target_turn_id FROM agent_messages WHERE to_peer=?",
                (agent_id,),
            ).fetchall()
        self.assertLessEqual(len(turn["prompt"].encode("utf-8")), 60 * 1024)
        selected = [row for row in rows if row["target_turn_id"] == turn["id"]]
        overflow = [row for row in rows if row["target_turn_id"] is None]
        self.assertEqual(len(selected), delivered)
        self.assertEqual(len(overflow), 30 - delivered)
        for row in rows:
            # Claimed but not delivered: marking happens when the turn starts.
            self.assertEqual(row["state"], "pending")


class ServerTimeoutTest(unittest.TestCase):
    """P1-6: MCP server wait-like ops honor the requested timeout + margin."""

    @staticmethod
    def _capture_timeout() -> tuple[list[float], mock.Mock]:
        captured: list[float] = []

        def _capture(payload: dict, timeout: float = 65) -> dict:
            captured.append(timeout)
            return {}

        request = mock.patch.object(mcp_server, "_request", side_effect=_capture)
        return captured, request

    def test_server_hub_wait_uses_requested_timeout(self):
        captured, request = self._capture_timeout()
        with mock.patch.object(mcp_server, "_ensure_daemon", return_value=None), (
            mock.patch.object(mcp_server, "_state", return_value={"control_port": 1})
        ), request:
            mcp_server.call_tool("hub", {"op": "wait", "timeout_seconds": 200})
        self.assertEqual(captured, [205])

    def test_server_wait_any_uses_requested_timeout(self):
        captured, request = self._capture_timeout()
        with mock.patch.object(mcp_server, "_ensure_daemon", return_value=None), (
            mock.patch.object(mcp_server, "_state", return_value={"control_port": 1})
        ), request:
            mcp_server.call_tool("wait_any", {"agent_ids": [], "timeout_seconds": 150})
        self.assertEqual(captured, [155])


class GrokHubTimeoutTest(unittest.TestCase):
    """P1-6: grok_hub socket timeout covers the requested wait duration."""

    @staticmethod
    def _blocked_create_connection(captured: dict) -> mock.Mock:
        def _block(address, timeout=None, **kwargs):
            captured["timeout"] = timeout
            raise OSError("unreachable")

        return mock.patch("grok_hub.socket.create_connection", side_effect=_block)

    def test_grok_hub_wait_socket_timeout_exceeds_requested_wait(self):
        captured: dict = {}
        with self._blocked_create_connection(captured):
            with self.assertRaises(RuntimeError):
                grok_hub.request(
                    47832,
                    {"worker_id": "w", "worker_token": "t", "op": "wait", "timeout_seconds": 200},
                )
        self.assertEqual(captured["timeout"], 205)

    def test_grok_hub_non_wait_uses_fixed_timeout(self):
        captured: dict = {}
        with self._blocked_create_connection(captured):
            with self.assertRaises(RuntimeError):
                grok_hub.request(
                    47832,
                    {"worker_id": "w", "worker_token": "t", "op": "list"},
                )
        self.assertEqual(captured["timeout"], grok_hub.REQUEST_TIMEOUT)


class WaitAnyIsolationTest(_IsolatedDbMixin, unittest.TestCase):
    """P1-7: wait_any agent ids and the from filter are thread-scoped."""

    def test_wait_any_cross_thread_same_as_unknown(self):
        b_agent, _ = self._seed_worker("B", name="b", status="completed")
        for aid in (b_agent, str(uuid.uuid4())):
            with self.assertRaisesRegex(ValueError, "agent not found"):
                daemon.action(
                    "wait_any",
                    {"agent_ids": [aid], "timeout_seconds": 1},
                    {"codex_thread_id": "A"},
                )

    def test_wait_any_from_cross_thread_rejected(self):
        b_agent, _ = self._seed_worker("B", name="b", status="completed")
        with self.assertRaisesRegex(ValueError, "peer not found"):
            daemon.action(
                "wait_any",
                {"agent_ids": [], "timeout_seconds": 1, "from": b_agent},
                {"codex_thread_id": "A"},
            )


class RetentionLifecycleTest(_IsolatedDbMixin, unittest.TestCase):
    """P1-8: tasks survive cleanup/delete while mailbox history exists."""

    def test_cleanup_with_historical_messages_does_not_crash(self):
        agent_id, _ = self._seed_worker("T", status="completed")
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET created_at=?,updated_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", agent_id),
            )
        self._seed_message("T", to_peer=agent_id, body="history one")
        self._seed_message("T", to_peer=agent_id, body="history two")

        prev_retention = daemon.RETENTION_DAYS
        daemon.RETENTION_DAYS = 0
        try:
            daemon.cleanup()
        finally:
            daemon.RETENTION_DAYS = prev_retention

        with daemon.connect() as db:
            agent = db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone()
            task = db.execute("SELECT 1 FROM tasks WHERE thread_id=?", ("T",)).fetchone()
            messages = db.execute(
                "SELECT 1 FROM agent_messages WHERE thread_id=?", ("T",)
            ).fetchall()
        self.assertIsNone(agent)
        self.assertIsNotNone(task)
        self.assertEqual(len(messages), 2)


class DeleteEndpointTest(_IsolatedDbMixin, _ViewerHttpMixin, unittest.TestCase):
    """P1-8: deleting the last agent keeps the task while messages exist."""

    def setUp(self):
        super().setUp()
        self._start_viewer()

    def tearDown(self):
        self._stop_viewer()
        super().tearDown()

    def test_manual_delete_last_agent_keeps_task_with_messages(self):
        agent_id, _ = self._seed_worker("T", status="completed")
        self._seed_message("T", to_peer=agent_id, body="keep me")

        status, body = self._post(f"/api/agents/{agent_id}/delete")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], True)

        with daemon.connect() as db:
            agent = db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone()
            task = db.execute("SELECT 1 FROM tasks WHERE thread_id=?", ("T",)).fetchone()
            messages = db.execute(
                "SELECT 1 FROM agent_messages WHERE thread_id=?", ("T",)
            ).fetchall()
        self.assertIsNone(agent)
        self.assertIsNotNone(task)
        self.assertEqual(len(messages), 1)


class MailboxCallbackTest(_IsolatedDbMixin, unittest.TestCase):
    """P1-3: the mailbox commit hook fires once, after the row is durable."""

    def test_mailbox_send_invokes_commit_callback(self):
        self._seed_task("A")
        calls: list[tuple[str, bool]] = []

        def _committed(message) -> None:
            with daemon.connect() as db:
                visible = (
                    db.execute(
                        "SELECT 1 FROM agent_messages WHERE id=?", (message.id,)
                    ).fetchone()
                    is not None
                )
            calls.append((message.id, visible))

        mailbox = Mailbox(
            daemon.coordination_connect, daemon.now, on_message_committed=_committed
        )
        sent = mailbox.send(thread_id="A", from_peer="main:A", to_peer="worker-x", body="hi")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], sent.id)
        # The callback runs post-commit: a fresh connection sees the row.
        self.assertTrue(calls[0][1])

        # A rejected send never reaches the callback.
        with self.assertRaises(ValueError):
            mailbox.send(thread_id="A", from_peer="main:A", to_peer="worker-x", body="   ")
        self.assertEqual(len(calls), 1)

    def test_delivery_marking_on_turn_start(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        turn_id = self._seed_turn(agent_id, turn_no=2, status="queued")
        self._seed_message("A", to_peer=agent_id, body="m1", target_turn_id=turn_id)
        self._seed_message("A", to_peer=agent_id, body="m2", target_turn_id=turn_id)

        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        marked = mailbox.mark_delivered_for_turn(turn_id=turn_id)
        self.assertEqual(marked, 2)
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT state,delivered_at FROM agent_messages WHERE to_peer=?",
                (agent_id,),
            ).fetchall()
        for row in rows:
            self.assertEqual(row["state"], "delivered")
            self.assertIsNotNone(row["delivered_at"])


if __name__ == "__main__":
    unittest.main()
