"""Tests for the durable agent coordination kernel (registry, mailbox, hub).

Covers thread-scoped peer visibility, durable SQLite messages, atomic drains,
no-lost-wakeup waiting, MCP tool wiring, and old-schema migration.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import daemon
import server as mcp_server
from coordination import AgentRegistry, CoordinationHub, Mailbox, main_peer_id
from coordination.types import MAX_MESSAGE_BYTES


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

    def _seed_agent(self, thread_id: str, name: str = "a", status: str = "queued") -> str:
        """Insert a worker row directly; returns its agent id. Never spawns Grok."""
        agent_id = str(uuid.uuid4())
        self._seed_task(thread_id)
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,display_title,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (agent_id, thread_id, name, ".", agent_id, status, name, stamp, stamp),
            )
        return agent_id

    def _make_hub(self) -> CoordinationHub:
        registry = AgentRegistry(daemon.coordination_connect)
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        return CoordinationHub(registry, mailbox)

    def _message_count(self) -> int:
        with daemon.connect() as db:
            return int(db.execute("SELECT COUNT(*) AS c FROM agent_messages").fetchone()["c"])


class CoordinationKernelTest(_IsolatedDbMixin, unittest.TestCase):
    def test_list_returns_only_same_thread_workers(self):
        a1 = self._seed_agent("A", name="a1")
        a2 = self._seed_agent("A", name="a2")
        b1 = self._seed_agent("B", name="b1")
        registry = AgentRegistry(daemon.coordination_connect)
        peers = registry.list_workers("A")
        self.assertEqual({p.id for p in peers}, {a1, a2})
        self.assertNotIn(b1, [p.id for p in peers])
        self.assertTrue(all(p.thread_id == "A" and p.kind == "worker" for p in peers))

    def test_cross_thread_resolve_returns_none(self):
        a1 = self._seed_agent("A", name="a1")
        b1 = self._seed_agent("B", name="b1")
        registry = AgentRegistry(daemon.coordination_connect)
        self.assertIsNone(registry.resolve_worker("A", "does-not-exist"))
        self.assertIsNone(registry.resolve_worker("A", b1))
        self.assertEqual(registry.resolve_worker("A", a1).id, a1)

    def test_send_persists_exactly_one_row(self):
        a1 = self._seed_agent("A", name="a1")
        hub = self._make_hub()
        result = hub.handle_main(thread_id="A", args={"op": "send", "to": a1, "message": "hello"})
        with daemon.connect() as db:
            rows = db.execute("SELECT * FROM agent_messages").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["id"])
        self.assertEqual(row["from_peer"], "main:A")
        self.assertEqual(row["to_peer"], a1)
        self.assertEqual(row["thread_id"], "A")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(result["message_id"], row["id"])
        self.assertEqual(result["from"], "main:A")
        self.assertEqual(result["to"], a1)
        self.assertEqual(result["state"], "pending")

    def test_reply_to_persists(self):
        a1 = self._seed_agent("A", name="a1")
        hub = self._make_hub()
        m1 = hub.handle_main(thread_id="A", args={"op": "send", "to": a1, "message": "first"})
        m2 = hub.handle_main(
            thread_id="A",
            args={"op": "send", "to": a1, "message": "second", "reply_to": m1["message_id"]},
        )
        with daemon.connect() as db:
            row = db.execute(
                "SELECT reply_to FROM agent_messages WHERE id=?", (m2["message_id"],)
            ).fetchone()
        self.assertEqual(row["reply_to"], m1["message_id"])

    def test_peek_does_not_consume(self):
        a1 = self._seed_agent("A", name="a1")
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        sent = mailbox.send(thread_id="A", from_peer=a1, to_peer="main:A", body="note")
        first = mailbox.inbox(peer_id="main:A", peek=True)
        second = mailbox.inbox(peer_id="main:A", peek=True)
        self.assertEqual([m.id for m in first], [sent.id])
        self.assertEqual([m.id for m in second], [sent.id])
        with daemon.connect() as db:
            row = db.execute(
                "SELECT state, consumed_at FROM agent_messages WHERE id=?", (sent.id,)
            ).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["consumed_at"])

    def test_drain_consumes_once(self):
        a1 = self._seed_agent("A", name="a1")
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        sent = mailbox.send(thread_id="A", from_peer=a1, to_peer="main:A", body="note")
        drained = mailbox.inbox(peer_id="main:A", peek=False)
        self.assertEqual([m.id for m in drained], [sent.id])
        again = mailbox.inbox(peer_id="main:A", peek=False)
        self.assertEqual(again, [])
        with daemon.connect() as db:
            row = db.execute(
                "SELECT state, consumed_at FROM agent_messages WHERE id=?", (sent.id,)
            ).fetchone()
        self.assertEqual(row["state"], "consumed")
        self.assertIsNotNone(row["consumed_at"])

    def test_two_concurrent_drains_never_duplicate(self):
        a1 = self._seed_agent("A", name="a1")
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        sent_ids = {
            mailbox.send(thread_id="A", from_peer=a1, to_peer="main:A", body=f"m{i}").id
            for i in range(10)
        }
        barrier = threading.Barrier(2)
        results: list[list[str]] = []
        lock = threading.Lock()

        def _drain() -> None:
            barrier.wait(timeout=10)
            got = mailbox.inbox(peer_id="main:A", peek=False)
            with lock:
                results.append([m.id for m in got])

        threads = [threading.Thread(target=_drain) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(len(results), 2)
        first_ids, second_ids = results
        self.assertEqual(set(first_ids) & set(second_ids), set())
        union = set(first_ids) | set(second_ids)
        self.assertLessEqual(len(union), 10)
        self.assertTrue(union.issubset(sent_ids))

    def test_wait_returns_buffered_message_immediately(self):
        a1 = self._seed_agent("A", name="a1")
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        sent = mailbox.send(thread_id="A", from_peer=a1, to_peer="main:A", body="buffered")
        started = time.monotonic()
        msg = mailbox.wait(peer_id="main:A", timeout_seconds=1)
        elapsed = time.monotonic() - started
        self.assertIsNotNone(msg)
        self.assertEqual(msg.id, sent.id)
        self.assertEqual(msg.kind, "message")
        self.assertLess(elapsed, 0.5)

    def test_wait_wakes_after_new_send(self):
        a1 = self._seed_agent("A", name="a1")
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        started = threading.Event()
        results: list[object] = []

        def _waiter() -> None:
            started.set()
            results.append(mailbox.wait(peer_id="main:A", timeout_seconds=5))

        began = time.monotonic()
        t = threading.Thread(target=_waiter)
        t.start()
        self.assertTrue(started.wait(timeout=2))
        time.sleep(0.05)
        sent = mailbox.send(thread_id="A", from_peer=a1, to_peer="main:A", body="wake me")
        t.join(timeout=5)
        elapsed = time.monotonic() - began
        self.assertFalse(t.is_alive())
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0])
        self.assertEqual(results[0].id, sent.id)
        self.assertLess(elapsed, 3.0)

    def test_lost_wakeup_stress(self):
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        delays = (0.0, 0.001, 0.002)
        for i in range(100):
            thread_id = f"stress-{i}"
            self._seed_task(thread_id)
            peer = main_peer_id(thread_id)
            barrier = threading.Barrier(2)
            results: list[object] = []

            def _waiter() -> None:
                barrier.wait(timeout=5)
                results.append(mailbox.wait(peer_id=peer, timeout_seconds=5))

            t = threading.Thread(target=_waiter, daemon=True)
            t.start()
            barrier.wait(timeout=5)
            time.sleep(delays[i % 3])
            sent = mailbox.send(thread_id=thread_id, from_peer=f"w-{i}", to_peer=peer, body=f"stress-{i}")
            t.join(timeout=6)
            self.assertFalse(t.is_alive(), f"iteration {i}: waiter did not return")
            self.assertEqual(len(results), 1, f"iteration {i}: waiter returned nothing")
            self.assertEqual(results[0].id, sent.id, f"iteration {i}: wrong message id")

    def test_twenty_concurrent_sends(self):
        a1 = self._seed_agent("A", name="a1")
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        barrier = threading.Barrier(20)
        results: list[object] = []
        lock = threading.Lock()

        def _sender(i: int) -> None:
            barrier.wait(timeout=10)
            msg = mailbox.send(thread_id="A", from_peer="main:A", to_peer=a1, body=f"body-{i}")
            with lock:
                results.append(msg)

        threads = [threading.Thread(target=_sender, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(len(results), 20)
        self.assertEqual(len({m.id for m in results}), 20)
        self.assertEqual(len({m.body for m in results}), 20)
        with daemon.connect() as db:
            rows = db.execute("SELECT id, body FROM agent_messages").fetchall()
        self.assertEqual(len(rows), 20)
        self.assertEqual(len({r["id"] for r in rows}), 20)
        self.assertEqual({r["body"] for r in rows}, {f"body-{i}" for i in range(20)})

    def test_empty_message_rejected(self):
        a1 = self._seed_agent("A", name="a1")
        hub = self._make_hub()
        with self.assertRaises(ValueError):
            hub.handle_main(thread_id="A", args={"op": "send", "to": a1, "message": "   "})
        self.assertEqual(self._message_count(), 0)

    def test_oversized_message_rejected(self):
        a1 = self._seed_agent("A", name="a1")
        hub = self._make_hub()
        body = "x" * (MAX_MESSAGE_BYTES + 1)
        with self.assertRaises(ValueError):
            hub.handle_main(thread_id="A", args={"op": "send", "to": a1, "message": body})
        self.assertEqual(self._message_count(), 0)

    def test_old_database_is_migrated(self):
        # Replace the fresh DB with an old-schema DB, then re-run init_db().
        for suffix in ("", "-wal", "-shm"):
            path = daemon.DATA / (daemon.DB_PATH.name + suffix)
            path.unlink(missing_ok=True)
        old = sqlite3.connect(str(daemon.DB_PATH))
        try:
            old.executescript(
                """
                CREATE TABLE tasks(
                  thread_id TEXT PRIMARY KEY, title TEXT, cwd TEXT, origin TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE agents(
                  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                  name TEXT NOT NULL, cwd TEXT NOT NULL, grok_session_id TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at)
                VALUES('old-thread','old','.','test','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
                INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at)
                VALUES('old-agent','old-thread','old','.','old-agent','queued',
                       '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
                """
            )
            old.commit()
        finally:
            old.close()

        daemon.init_db()

        with daemon.connect() as db:
            tables = {
                r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertIn("agent_messages", tables)
            leading_columns = set()
            for index_row in db.execute("PRAGMA index_list(agent_messages)"):
                index_name = index_row["name"]
                cols = db.execute(f'PRAGMA index_info("{index_name}")').fetchall()
                if cols:
                    leading_columns.add(cols[0][2])
            self.assertLessEqual({"to_peer", "thread_id", "reply_to"}, leading_columns)
            tasks = db.execute("SELECT * FROM tasks").fetchall()
            agents = db.execute("SELECT * FROM agents").fetchall()
        self.assertEqual([r["thread_id"] for r in tasks], ["old-thread"])
        self.assertEqual([r["id"] for r in agents], ["old-agent"])

    def test_hub_in_mcp_tools_and_old_tools_intact(self):
        names = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertEqual(
            names,
            {"create_agent", "send", "update_agent", "status", "wait", "result", "cancel", "signoff", "hub", "create_agents", "wait_any"},
        )
        hub = next(tool for tool in mcp_server.TOOLS if tool["name"] == "hub")
        properties = hub["inputSchema"]["properties"]
        self.assertEqual(set(properties["op"]["enum"]), {"list", "send", "inbox", "wait"})
        self.assertNotIn("thread_id", properties)

    def test_mcp_context_forwarding_for_hub(self):
        captured: list[dict] = []

        def _capture(payload: dict, timeout: float = 65) -> dict:
            captured.append(payload)
            return {}

        prev_thread = os.environ.get("CODEX_THREAD_ID")
        os.environ["CODEX_THREAD_ID"] = "thread-A"
        try:
            with mock.patch.object(mcp_server, "_state", return_value={"control_port": 1}), (
                mock.patch.object(mcp_server, "_request", side_effect=_capture)
            ):
                mcp_server.call_tool("hub", {"op": "list"})
        finally:
            if prev_thread is None:
                os.environ.pop("CODEX_THREAD_ID", None)
            else:
                os.environ["CODEX_THREAD_ID"] = prev_thread
        hub_payload = next(p for p in captured if p.get("action") == "hub")
        self.assertEqual(hub_payload["action"], "hub")
        self.assertEqual(hub_payload["context"]["codex_thread_id"], "thread-A")
        self.assertEqual(hub_payload["args"]["op"], "list")

    def test_hub_action_dispatch_in_daemon(self):
        a1 = self._seed_agent("thread-A", name="a1")
        a2 = self._seed_agent("thread-A", name="a2")
        self._seed_agent("thread-B", name="b1")
        result = daemon.action("hub", {"op": "list"}, {"codex_thread_id": "thread-A"})
        self.assertEqual(result["caller"], "main:thread-A")
        self.assertEqual({p["id"] for p in result["peers"]}, {a1, a2})
        fallback = daemon.action("hub", {"op": "list"}, {})
        self.assertEqual(fallback["caller"], "main:unknown")
        self.assertEqual(fallback["peers"], [])

    def test_hub_send_cross_thread_fails_like_unknown(self):
        self._seed_agent("thread-A", name="a1")
        b1 = self._seed_agent("thread-B", name="b1")
        with self.assertRaises(ValueError):
            daemon.action(
                "hub",
                {"op": "send", "to": b1, "message": "hi"},
                {"codex_thread_id": "thread-A"},
            )
        self.assertEqual(self._message_count(), 0)

    def test_wait_timeout_returns_normal_result(self):
        self._seed_task("thread-A")
        result = daemon.action(
            "hub", {"op": "wait", "timeout_seconds": 0}, {"codex_thread_id": "thread-A"}
        )
        self.assertEqual(result, {"kind": "timeout"})
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        self.assertIsNone(mailbox.wait(peer_id="main:thread-A", timeout_seconds=0))


if __name__ == "__main__":
    unittest.main()
