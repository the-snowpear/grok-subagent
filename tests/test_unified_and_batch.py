"""Tests for PR C: batch agent spawn, unified wait_any, and the mailbox peek surface.

Covers create_agents (shared-thread batch spawn with per-item validation and
limit enforcement), wait_any (first of {mailbox message, agent terminal,
timeout} with no lost wakeups), and the non-consuming peek_one/revision
mailbox API used by the unified wait loop.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import daemon
from coordination import main_peer_id


ROOT = Path(__file__).resolve().parents[1]
FAKE_GROK = ROOT / "tests" / "fake_grok.py"


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

    def _seed_message_to_main(self, thread_id: str, body: str) -> None:
        """Deliver a message into the main peer's mailbox for a thread."""
        self._seed_task(thread_id)
        daemon.MAILBOX.send(
            thread_id=thread_id,
            from_peer="peer-x",
            to_peer=main_peer_id(thread_id),
            body=body,
        )


class UnifiedWaitAndBatchTest(_IsolatedDbMixin, unittest.TestCase):
    """PR C: batch spawn, unified wait, non-consuming mailbox peek."""

    def setUp(self):
        super().setUp()
        # Let batches create many agents without tripping the product limits.
        self._orig_limits = {
            "MAX_ACTIVE_PER_THREAD": daemon.MAX_ACTIVE_PER_THREAD,
            "MAX_ACTIVE_AGENTS": daemon.MAX_ACTIVE_AGENTS,
        }
        daemon.MAX_ACTIVE_PER_THREAD = 20
        daemon.MAX_ACTIVE_AGENTS = 100
        # Real creations run the deterministic fake Grok CLI.
        self._prev_fake_grok = os.environ.get("GROK_OBSERVER_FAKE_GROK")
        self._prev_fake_duration = os.environ.get("GROK_FAKE_DURATION")
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "0.02"

    def tearDown(self):
        for key, value in self._orig_limits.items():
            setattr(daemon, key, value)
        if self._prev_fake_grok is None:
            os.environ.pop("GROK_OBSERVER_FAKE_GROK", None)
        else:
            os.environ["GROK_OBSERVER_FAKE_GROK"] = self._prev_fake_grok
        if self._prev_fake_duration is None:
            os.environ.pop("GROK_FAKE_DURATION", None)
        else:
            os.environ["GROK_FAKE_DURATION"] = self._prev_fake_duration
        super().tearDown()

    def test_create_agents_creates_all(self):
        items = [
            {"agent_name": "a1", "prompt": "do one", "cwd": str(self.folder)},
            {"agent_name": "a2", "prompt": "do two", "cwd": str(self.folder)},
            {"agent_name": "a3", "prompt": "do three", "cwd": str(self.folder)},
        ]
        result = daemon.action(
            "create_agents",
            {"agents": items},
            {"codex_thread_id": "T", "codex_origin": "test"},
        )
        self.assertEqual(result["created"], 3)
        self.assertEqual(result["errors"], [])
        agents = result["agents"]
        self.assertEqual(len(agents), 3)
        ids = [entry["agent_id"] for entry in agents]
        self.assertEqual(len(set(ids)), 3)
        for entry in agents:
            self.assertIn("agent_id", entry)
            self.assertIn("status", entry)
            self.assertIn("viewer_url", entry)
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT id, thread_id FROM agents WHERE thread_id=?", ("T",)
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["id"] for row in rows}, set(ids))
        self.assertTrue(all(row["thread_id"] == "T" for row in rows))

    def test_create_agents_validation(self):
        with self.assertRaises(ValueError) as ctx:
            daemon.action("create_agents", {"agents": []}, {"codex_thread_id": "T"})
        self.assertIn("agents must be a non-empty list", str(ctx.exception))

        many = [{"agent_name": f"a{i}", "prompt": f"p{i}"} for i in range(21)]
        with self.assertRaises(ValueError) as ctx:
            daemon.action("create_agents", {"agents": many}, {"codex_thread_id": "T"})
        self.assertIn("at most 20 agents per batch", str(ctx.exception))

        mixed = [
            {"agent_name": "ok1", "prompt": "fine", "cwd": str(self.folder)},
            {"agent_name": "no-prompt", "cwd": str(self.folder)},
            {"agent_name": "ok2", "prompt": "fine too", "cwd": str(self.folder)},
        ]
        result = daemon.action(
            "create_agents",
            {"agents": mixed},
            {"codex_thread_id": "T", "codex_origin": "test"},
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["index"], 1)
        self.assertIn("agent_name and prompt are required", result["errors"][0]["error"])
        created = {entry["agent_id"] for entry in result["agents"]}
        with daemon.connect() as db:
            rows = db.execute("SELECT id FROM agents WHERE thread_id=?", ("T",)).fetchall()
        self.assertEqual({row["id"] for row in rows}, created)

    def test_create_agents_respects_per_thread_limit(self):
        # Longer fake duration keeps the first two agents active while the
        # rest of the batch is processed, so the limit check is deterministic.
        os.environ["GROK_FAKE_DURATION"] = "0.5"
        daemon.MAX_ACTIVE_PER_THREAD = 2
        items = [
            {"agent_name": f"a{i}", "prompt": f"p{i}", "cwd": str(self.folder)}
            for i in range(4)
        ]
        result = daemon.action(
            "create_agents",
            {"agents": items},
            {"codex_thread_id": "T", "codex_origin": "test"},
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(result["errors"]), 2)
        for err in result["errors"]:
            self.assertIn("上限", err["error"])
        with daemon.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM agents WHERE thread_id=?", ("T",)
            ).fetchone()["c"]
        self.assertEqual(int(count), 2)

    def test_create_agents_shared_thread_context(self):
        batch1 = [
            {"agent_name": f"t1-{i}", "prompt": f"p{i}", "cwd": str(self.folder)}
            for i in range(2)
        ]
        batch2 = [
            {"agent_name": f"t2-{i}", "prompt": f"p{i}", "cwd": str(self.folder)}
            for i in range(2)
        ]
        r1 = daemon.action(
            "create_agents", {"agents": batch1}, {"codex_thread_id": "T1", "codex_origin": "test"}
        )
        r2 = daemon.action(
            "create_agents", {"agents": batch2}, {"codex_thread_id": "T2", "codex_origin": "test"}
        )
        self.assertEqual(r1["created"], 2)
        self.assertEqual(r2["created"], 2)
        with daemon.connect() as db:
            rows = db.execute("SELECT id, thread_id FROM agents ORDER BY thread_id, name").fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["thread_id"], []).append(row["id"])
        self.assertEqual(set(grouped), {"T1", "T2"})
        self.assertEqual(len(grouped["T1"]), 2)
        self.assertEqual(len(grouped["T2"]), 2)
        self.assertEqual({entry["agent_id"] for entry in r1["agents"]}, set(grouped["T1"]))
        self.assertEqual({entry["agent_id"] for entry in r2["agents"]}, set(grouped["T2"]))

    def test_wait_any_returns_message(self):
        self._seed_message_to_main("T", "hello worker")
        result = daemon.action(
            "wait_any",
            {"agent_ids": [], "timeout_seconds": 5},
            {"codex_thread_id": "T"},
        )
        self.assertEqual(result["kind"], "message")
        self.assertEqual(result["message"]["body"], "hello worker")
        # Peek again: the unified wait must NOT have consumed the message.
        msg = daemon.MAILBOX.peek_one(peer_id=main_peer_id("T"))
        self.assertIsNotNone(msg)
        self.assertEqual(msg.body, "hello worker")

    def test_wait_any_returns_agent_terminal(self):
        aid = self._seed_agent("T", status="completed")
        result = daemon.action(
            "wait_any",
            {"agent_ids": [aid], "timeout_seconds": 5},
            {"codex_thread_id": "T"},
        )
        self.assertEqual(result["kind"], "agent")
        self.assertEqual(result["agent_id"], aid)
        self.assertEqual(result["status"], "completed")
        self.assertIn("revision", result)
        self.assertIn("turns", result)

    def test_wait_any_timeout(self):
        aid = self._seed_agent("T", status="queued")
        started = time.monotonic()
        result = daemon.action(
            "wait_any",
            {"agent_ids": [aid], "timeout_seconds": 1},
            {"codex_thread_id": "T"},
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result["kind"], "timeout")
        self.assertLess(elapsed, 3.0)

    def test_wait_any_unknown_agent_raises(self):
        with self.assertRaises(ValueError) as ctx:
            daemon.action(
                "wait_any",
                {"agent_ids": ["does-not-exist"], "timeout_seconds": 1},
                {"codex_thread_id": "T"},
            )
        self.assertIn("agent not found", str(ctx.exception))

    def test_wait_any_no_lost_wakeup(self):
        delays = (0.0, 0.001, 0.002)
        for i in range(40):
            thread_id = f"wake-{i}"
            self._seed_task(thread_id)
            peer = main_peer_id(thread_id)
            barrier = threading.Barrier(2)
            results: list[object] = []

            def _waiter() -> None:
                barrier.wait(timeout=5)
                results.append(
                    daemon.action(
                        "wait_any",
                        {"agent_ids": [], "timeout_seconds": 5},
                        {"codex_thread_id": thread_id},
                    )
                )

            t = threading.Thread(target=_waiter, daemon=True)
            t.start()
            barrier.wait(timeout=5)
            time.sleep(delays[i % 3])
            daemon.MAILBOX.send(
                thread_id=thread_id,
                from_peer=f"w-{i}",
                to_peer=peer,
                body=f"wake-{i}",
            )
            t.join(timeout=6)
            self.assertFalse(t.is_alive(), f"iteration {i}: waiter did not return")
            self.assertEqual(len(results), 1, f"iteration {i}: waiter returned nothing")
            self.assertEqual(results[0]["kind"], "message", f"iteration {i}: {results[0]!r}")
            self.assertEqual(results[0]["message"]["body"], f"wake-{i}")

    def test_wait_any_agent_completion_wakes(self):
        aid = self._seed_agent("T", status="queued")
        results: list[object] = []
        started = time.monotonic()

        def _waiter() -> None:
            results.append(
                daemon.action(
                    "wait_any",
                    {"agent_ids": [aid], "timeout_seconds": 5},
                    {"codex_thread_id": "T"},
                )
            )

        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        time.sleep(0.05)
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET status='completed', updated_at=? WHERE id=?",
                (daemon.now(), aid),
            )
        t.join(timeout=3)
        elapsed = time.monotonic() - started
        self.assertFalse(t.is_alive(), "waiter did not wake on agent completion")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["kind"], "agent")
        self.assertEqual(results[0]["agent_id"], aid)
        self.assertEqual(results[0]["status"], "completed")
        self.assertLess(elapsed, 3.0)

    def test_peek_one_non_consuming(self):
        self._seed_message_to_main("T", "peek me")
        peer = main_peer_id("T")
        first = daemon.MAILBOX.peek_one(peer_id=peer)
        second = daemon.MAILBOX.peek_one(peer_id=peer)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.body, "peek me")
        with daemon.connect() as db:
            row = db.execute(
                "SELECT state FROM agent_messages WHERE id=?", (first.id,)
            ).fetchone()
        self.assertEqual(row["state"], "pending")

    def test_create_agent_single_unchanged(self):
        data = daemon.action(
            "create_agent",
            {"agent_name": "a", "prompt": "p", "cwd": str(self.folder)},
            {"codex_thread_id": "T", "codex_origin": "test"},
        )
        self.assertEqual(set(data), {"agent_id", "status", "viewer_url", "browser_opened"})
        self.assertEqual(data["status"], "queued")
        self.assertIn(data["agent_id"], data["viewer_url"])

    def test_wait_any_message_priority(self):
        self._seed_message_to_main("T", "priority message")
        aid = self._seed_agent("T", status="completed")
        result = daemon.action(
            "wait_any",
            {"agent_ids": [aid], "timeout_seconds": 5},
            {"codex_thread_id": "T"},
        )
        self.assertEqual(result["kind"], "message")
        self.assertEqual(result["message"]["body"], "priority message")


if __name__ == "__main__":
    unittest.main()
