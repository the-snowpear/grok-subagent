"""Tests for the worker bridge (PR B): capability tokens, worker hub ops,
coalesced delivery to completed workers, worker env injection, and the
grok_hub CLI request builder.

Covers token-backed worker authentication, thread-scoped worker peer ops,
durable send/inbox/wait flows, one-transaction coalesced follow-up delivery,
idempotent delivery, and the CLI request payload shape.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import daemon
from coordination import AgentRegistry, CoordinationHub, Mailbox, main_peer_id
from coordination.types import MAX_MESSAGE_BYTES
from grok_hub import build_request


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
    ) -> str:
        """Insert one pending agent_message directly; returns its id."""
        if from_peer is None:
            from_peer = main_peer_id(thread_id)
        if created_at is None:
            created_at = daemon.now()
        if message_id is None:
            message_id = str(uuid.uuid4())
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,body,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (message_id, thread_id, from_peer, to_peer, body, created_at),
            )
        return message_id

    def _make_hub(self) -> CoordinationHub:
        registry = AgentRegistry(daemon.coordination_connect)
        mailbox = Mailbox(daemon.coordination_connect, daemon.now)
        return CoordinationHub(registry, mailbox)

    def _message_count(self) -> int:
        with daemon.connect() as db:
            return int(db.execute("SELECT COUNT(*) AS c FROM agent_messages").fetchone()["c"])

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


class WorkerBridgeTest(_IsolatedDbMixin, unittest.TestCase):
    def test_create_agent_stores_unique_token(self):
        prev_fake = os.environ.get("GROK_OBSERVER_FAKE_GROK")
        prev_duration = os.environ.get("GROK_FAKE_DURATION")
        os.environ["GROK_OBSERVER_FAKE_GROK"] = str(FAKE_GROK)
        os.environ["GROK_FAKE_DURATION"] = "0.02"
        try:
            first = daemon.action(
                "create_agent",
                {"agent_name": "w1", "prompt": "do work", "cwd": str(self.folder)},
                {"codex_thread_id": "t"},
            )
            second = daemon.action(
                "create_agent",
                {"agent_name": "w2", "prompt": "do work", "cwd": str(self.folder)},
                {"codex_thread_id": "t"},
            )
        finally:
            if prev_duration is None:
                os.environ.pop("GROK_FAKE_DURATION", None)
            else:
                os.environ["GROK_FAKE_DURATION"] = prev_duration
            if prev_fake is None:
                os.environ.pop("GROK_OBSERVER_FAKE_GROK", None)
            else:
                os.environ["GROK_OBSERVER_FAKE_GROK"] = prev_fake

        token_a = self._hub_token(first["agent_id"])
        token_b = self._hub_token(second["agent_id"])
        self.assertTrue(token_a)
        self.assertTrue(token_b)
        self.assertNotEqual(token_a, token_b)

    def test_worker_hub_rejects_bad_token(self):
        worker_id, _ = self._seed_worker("A", status="completed")
        with self.assertRaisesRegex(ValueError, "worker authentication failed"):
            daemon.worker_hub_request(worker_id, "wrong", {"op": "list"})
        # Unknown worker ids must fail identically, regardless of token.
        with self.assertRaisesRegex(ValueError, "worker authentication failed"):
            daemon.worker_hub_request("no-such-worker", "tok-anything", {"op": "list"})

    def test_worker_hub_accepts_valid_token(self):
        worker_id, token = self._seed_worker("A", status="completed")
        data = daemon.worker_hub_request(worker_id, token, {"op": "list"})
        self.assertEqual(data["caller"], worker_id)
        self.assertEqual(data["main"], "main:A")
        self.assertEqual([p["id"] for p in data["peers"]], [worker_id])
        self.assertEqual(data["peers"][0]["thread_id"], "A")
        self.assertEqual(data["peers"][0]["kind"], "worker")

    def test_worker_peers_same_thread_only(self):
        a1, _ = self._seed_worker("A", name="a1", status="completed")
        a2, _ = self._seed_worker("A", name="a2", status="completed")
        b1, _ = self._seed_worker("B", name="b1", status="completed")
        data = daemon.worker_hub_request(a1, self._hub_token(a1) or "", {"op": "list"})
        self.assertEqual(data["main"], "main:A")
        peer_ids = {p["id"] for p in data["peers"]}
        self.assertEqual(peer_ids, {a1, a2})
        self.assertNotIn(b1, peer_ids)

    def test_worker_send_to_main_persists_and_main_inbox_reads(self):
        worker_id, _ = self._seed_worker("A", status="completed")
        hub = self._make_hub()
        receipt = hub.handle_worker(
            worker_id=worker_id,
            args={"op": "send", "to": "main:A", "message": "hello main"},
        )
        self.assertEqual(receipt["to"], "main:A")
        self.assertEqual(receipt["from"], worker_id)
        self.assertEqual(receipt["state"], "pending")
        self.assertTrue(receipt["message_id"])
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT from_peer,to_peer,body FROM agent_messages WHERE to_peer='main:A'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["from_peer"], worker_id)
        self.assertEqual(rows[0]["to_peer"], "main:A")
        self.assertEqual(rows[0]["body"], "hello main")

        inbox = hub.handle_main(thread_id="A", args={"op": "inbox"})
        self.assertEqual(len(inbox["messages"]), 1)
        message = inbox["messages"][0]
        self.assertEqual(message["from_peer"], worker_id)
        self.assertEqual(message["to_peer"], "main:A")
        self.assertEqual(message["body"], "hello main")

    def test_worker_send_to_sibling_worker_persists(self):
        w1, _ = self._seed_worker("A", name="w1", status="completed")
        w2, _ = self._seed_worker("A", name="w2", status="completed")
        hub = self._make_hub()
        receipt = hub.handle_worker(
            worker_id=w1,
            args={"op": "send", "to": w2, "message": "hi sibling"},
        )
        self.assertEqual(receipt["to"], w2)
        self.assertEqual(receipt["from"], w1)
        with daemon.connect() as db:
            row = db.execute(
                "SELECT from_peer,to_peer,body FROM agent_messages WHERE id=?",
                (receipt["message_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["to_peer"], w2)
        self.assertEqual(row["from_peer"], w1)

    def test_worker_send_cross_thread_fails_like_unknown(self):
        w_a, _ = self._seed_worker("A", name="wa", status="completed")
        w_b, _ = self._seed_worker("B", name="wb", status="completed")
        hub = self._make_hub()
        # A cross-thread worker id must resolve exactly like an unknown id.
        with self.assertRaisesRegex(ValueError, "peer not found"):
            hub.handle_worker(
                worker_id=w_a,
                args={"op": "send", "to": w_b, "message": "sneaky"},
            )
        with self.assertRaisesRegex(ValueError, "peer not found"):
            hub.handle_worker(
                worker_id=w_a,
                args={"op": "send", "to": "does-not-exist", "message": "sneaky"},
            )
        self.assertEqual(self._message_count(), 0)

    def test_worker_send_empty_and_oversized_rejected(self):
        worker_id, _ = self._seed_worker("A", status="completed")
        hub = self._make_hub()
        with self.assertRaises(ValueError):
            hub.handle_worker(
                worker_id=worker_id,
                args={"op": "send", "to": "main:A", "message": "   "},
            )
        oversized = "x" * (MAX_MESSAGE_BYTES + 1)
        with self.assertRaises(ValueError):
            hub.handle_worker(
                worker_id=worker_id,
                args={"op": "send", "to": "main:A", "message": oversized},
            )
        self.assertEqual(self._message_count(), 0)

    def test_worker_inbox_peek_and_drain(self):
        worker_id, _ = self._seed_worker("A", status="completed")
        hub = self._make_hub()
        hub.handle_main(thread_id="A", args={"op": "send", "to": worker_id, "message": "m1"})
        hub.handle_main(thread_id="A", args={"op": "send", "to": worker_id, "message": "m2"})

        first_peek = hub.handle_worker(worker_id=worker_id, args={"op": "inbox", "peek": True})
        second_peek = hub.handle_worker(worker_id=worker_id, args={"op": "inbox", "peek": True})
        self.assertEqual(len(first_peek["messages"]), 2)
        self.assertEqual(len(second_peek["messages"]), 2)
        self.assertTrue(first_peek["peek"])

        drained = hub.handle_worker(worker_id=worker_id, args={"op": "inbox"})
        self.assertEqual(sorted(m["body"] for m in drained["messages"]), ["m1", "m2"])

        after = hub.handle_worker(worker_id=worker_id, args={"op": "inbox"})
        self.assertEqual(after["messages"], [])

        with daemon.connect() as db:
            rows = db.execute(
                "SELECT consumed_at FROM agent_messages WHERE to_peer=?", (worker_id,)
            ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIsNotNone(row["consumed_at"])

    def test_delivery_completed_agent_creates_one_coalesced_follow_up(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        bodies = ["body-one", "body-two", "body-three"]
        for i, body in enumerate(bodies, start=1):
            self._seed_message(
                "A",
                to_peer=agent_id,
                body=body,
                created_at=f"2000-01-01T00:00:0{i}+00:00",
            )

        runner = mock.Mock()
        with mock.patch.object(daemon, "get_runner", return_value=runner):
            delivered = daemon.maybe_schedule_delivery(agent_id)

        self.assertEqual(delivered, 3)
        runner.enqueue.assert_called_once()
        (turn_id, prompt), _ = runner.enqueue.call_args

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
        self.assertEqual(new_turn["id"], turn_id)
        self.assertIn("收到 3 条协作消息", new_turn["prompt"])
        for body in bodies:
            self.assertIn(body, new_turn["prompt"])
        self.assertEqual(agent["status"], "queued")
        self.assertEqual(len(messages), 3)
        for row in messages:
            # Durable delivery: messages stay pending until the turn actually
            # starts; the claim is the target_turn_id link.
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["target_turn_id"], turn_id)

    def test_delivery_running_agent_skipped(self):
        agent_id, _ = self._seed_worker("A", status="running")
        self._seed_turn(agent_id, turn_no=1, status="running")
        self._seed_message("A", to_peer=agent_id, body="ping")

        delivered = daemon.maybe_schedule_delivery(agent_id)

        self.assertEqual(delivered, 0)
        self.assertEqual(self._turn_count(agent_id), 1)
        with daemon.connect() as db:
            state = db.execute(
                "SELECT state FROM agent_messages WHERE to_peer=?", (agent_id,)
            ).fetchone()["state"]
            status = db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()["status"]
        self.assertEqual(state, "pending")
        self.assertEqual(status, "running")

    def test_delivery_no_pending_messages_noop(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")

        delivered = daemon.maybe_schedule_delivery(agent_id)

        self.assertEqual(delivered, 0)
        self.assertEqual(self._turn_count(agent_id), 1)
        with daemon.connect() as db:
            status = db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()["status"]
        self.assertEqual(status, "completed")

    def test_delivery_failed_agent_skipped(self):
        agent_id, _ = self._seed_worker("A", status="failed")
        self._seed_turn(agent_id, turn_no=1, status="failed")
        self._seed_message("A", to_peer=agent_id, body="ping")

        delivered = daemon.maybe_schedule_delivery(agent_id)

        self.assertEqual(delivered, 0)
        self.assertEqual(self._turn_count(agent_id), 1)
        with daemon.connect() as db:
            state = db.execute(
                "SELECT state FROM agent_messages WHERE to_peer=?", (agent_id,)
            ).fetchone()["state"]
            status = db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()["status"]
        self.assertEqual(state, "pending")
        self.assertEqual(status, "failed")

    def test_worker_bridge_env(self):
        worker_id, token = self._seed_worker("A", status="completed")
        env = daemon.worker_bridge_env(worker_id)
        self.assertEqual(
            env,
            {
                "GROK_OBSERVER_AGENT_ID": worker_id,
                "GROK_OBSERVER_AGENT_TOKEN": token,
                # Workers must get the worker control port, never the host port.
                "GROK_OBSERVER_WORKER_CONTROL_PORT": str(daemon.ACTUAL_WORKER_CONTROL_PORT),
            },
        )
        self.assertNotIn("GROK_OBSERVER_CONTROL_PORT", env)

        # A worker with a NULL hub_token gets one backfilled and persisted.
        worker_id, _ = self._seed_worker("A", name="notoken", status="completed")
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET hub_token=NULL WHERE id=?", (worker_id,)
            )
        env = daemon.worker_bridge_env(worker_id)
        self.assertEqual(env["GROK_OBSERVER_AGENT_ID"], worker_id)
        self.assertTrue(env["GROK_OBSERVER_AGENT_TOKEN"])
        self.assertEqual(env["GROK_OBSERVER_WORKER_CONTROL_PORT"], str(daemon.ACTUAL_WORKER_CONTROL_PORT))
        stored = self._hub_token(worker_id)
        self.assertTrue(stored)
        self.assertEqual(stored, env["GROK_OBSERVER_AGENT_TOKEN"])

    def test_cli_build_request(self):
        # The worker control protocol has no generic action field: identity
        # and op args sit at the top level of the request line.
        self.assertEqual(
            build_request("w", "t", {"op": "list"}),
            {"worker_id": "w", "worker_token": "t", "op": "list"},
        )
        self.assertEqual(
            build_request("w", "t", {"op": "send", "to": "main:A", "message": "hi"}),
            {
                "worker_id": "w",
                "worker_token": "t",
                "op": "send",
                "to": "main:A",
                "message": "hi",
            },
        )
        self.assertEqual(
            build_request("w", "t", {"op": "inbox", "peek": True})["op"], "inbox"
        )
        self.assertEqual(
            build_request("w", "t", {"op": "wait", "timeout_seconds": 5})["op"], "wait"
        )

    def test_worker_wait_wakes_on_main_send(self):
        worker_id, _ = self._seed_worker("A", status="completed")
        hub = self._make_hub()
        results: list = []

        def waiter() -> None:
            try:
                results.append(
                    hub.handle_worker(
                        worker_id=worker_id,
                        args={"op": "wait", "timeout_seconds": 5},
                    )
                )
            except Exception as exc:  # pragma: no cover - failure surfaced via join
                results.append(exc)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.2)
        hub.handle_main(
            thread_id="A",
            args={"op": "send", "to": worker_id, "message": "wake up"},
        )
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIsInstance(result, dict)
        self.assertEqual(result["kind"], "message")
        self.assertEqual(result["message"]["body"], "wake up")
        self.assertEqual(result["message"]["from_peer"], "main:A")

    def test_delivery_idempotent(self):
        agent_id, _ = self._seed_worker("A", status="completed")
        self._seed_turn(agent_id, turn_no=1, status="completed")
        self._seed_message("A", to_peer=agent_id, body="b1")
        self._seed_message("A", to_peer=agent_id, body="b2")

        runner = mock.Mock()
        with mock.patch.object(daemon, "get_runner", return_value=runner):
            first = daemon.maybe_schedule_delivery(agent_id)
            second = daemon.maybe_schedule_delivery(agent_id)

        self.assertEqual(first, 2)
        self.assertEqual(second, 0)
        runner.enqueue.assert_called_once()
        self.assertEqual(self._turn_count(agent_id), 2)
        with daemon.connect() as db:
            states = db.execute(
                "SELECT state FROM agent_messages WHERE to_peer=?", (agent_id,)
            ).fetchall()
            agent = db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
        self.assertEqual([row["state"] for row in states], ["pending", "pending"])
        self.assertEqual(agent["status"], "queued")


if __name__ == "__main__":
    unittest.main()
