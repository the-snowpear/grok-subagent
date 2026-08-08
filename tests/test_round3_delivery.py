"""Behavioral regression tests for delivery crash consistency (Round 3, commit 1).

Contract under test (daemon.py + coordination/mailbox.py):
- AgentRunner._run has ONE try covering begin_turn -> Popen -> child identity
  persistence -> reconcile. child_created = Popen returned a proc;
  delivery_started = child identity (agents.child_pid + turns.child_started_at)
  durably persisted.
- except: child_created -> reconcile(started=True); not delivery_started ->
  release claim + notify.
- The terminal phase reconciles started=child_created.
- recover() reconciles every pending linked turn by its turns.child_started_at
  marker (queued turns keep their claims; failed pre-spawn turns release them).
- recover_runners() filters cancelled agents out of the queued-turn query.
- Mailbox.reconcile_turn_delivery: started=True converges pending linked rows
  to delivered with COALESCE'd timestamps; started=False releases claims;
  idempotent; bounded busy retry on locked/busy.

Every assertion is behavioral (DB state, return values, message visibility
through the mailbox). No source-string inspection. Faults are injected by
patching daemon module-level symbols / AgentRunner methods so the runner
thread (or the synchronous _run drive) hits them.
"""

from __future__ import annotations

import unittest
from unittest import mock

import daemon
from tests.test_round2_review_fixes import Round2Mixin


class _FakeStream:
    """Empty stdout/stderr stand-in: iteration terminates, close is a no-op."""

    def __iter__(self):
        return iter(())

    def close(self) -> None:
        pass


class _FakeProc:
    """Controlled subprocess.Popen stand-in for runner fault injection.

    Mirrors the real Popen surface the runner touches: .pid, iterable
    .stdout/.stderr (empty so the stdout loop terminates), .wait() -> 0 and
    .poll() -> None.
    """

    pid = 424242
    stdout = _FakeStream()
    stderr = _FakeStream()

    def wait(self) -> int:
        return 0

    def poll(self) -> None:
        return None


class DeliveryCrashConsistencyTests(Round2Mixin, unittest.TestCase):
    def _schedule(self, thread_id: str, body: str = "one-shot") -> tuple[str, str, int, str]:
        """Seed a completed agent + pending message, claim them into a queued delivery turn.

        Returns (agent_id, message_id, turn_id, prompt).
        """
        aid = self.seed_agent(thread_id, "completed")
        mid = self.seed_message(thread_id, aid, body)
        with mock.patch.object(daemon, "get_runner", return_value=None):
            self.assertEqual(daemon.maybe_schedule_delivery(aid), 1)
        with daemon.connect() as db:
            turn = db.execute(
                "SELECT id,prompt FROM turns WHERE agent_id=? AND status='queued' ORDER BY id LIMIT 1",
                (aid,),
            ).fetchone()
        self.assertIsNotNone(turn)
        return aid, mid, int(turn["id"]), str(turn["prompt"])

    def _assert_released(self, aid: str, mid: str) -> None:
        """Pre-spawn failure outcome: message pending again and visible to inbox."""
        with daemon.connect() as db:
            msg = db.execute("SELECT state,target_turn_id FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(msg["state"], "pending")
        self.assertIsNone(msg["target_turn_id"])
        visible = daemon.MAILBOX.peek_one(peer_id=aid)
        self.assertIsNotNone(visible)
        self.assertEqual(visible.id, mid)

    def test_begin_turn_failure_releases_delivery_claim(self):
        thread_id = "r3-begin-fail"
        aid, mid, turn_id, prompt = self._schedule(thread_id)
        runner = daemon.get_runner(aid)
        with mock.patch.object(
            daemon.AgentRunner, "begin_turn", side_effect=RuntimeError("begin_turn injected failure")
        ):
            runner._run(turn_id, prompt)
        self._assert_released(aid, mid)
        with daemon.connect() as db:
            turn = db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
        self.assertIn(turn["status"], ("failed", "cancelled"))

    def test_workspace_snapshot_failure_releases_delivery_claim(self):
        thread_id = "r3-snapshot-fail"
        aid, mid, turn_id, prompt = self._schedule(thread_id)
        runner = daemon.get_runner(aid)
        with mock.patch.object(
            daemon, "workspace_snapshot", side_effect=RuntimeError("workspace_snapshot injected failure")
        ):
            runner._run(turn_id, prompt)
        self._assert_released(aid, mid)
        with daemon.connect() as db:
            turn = db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
        self.assertIn(turn["status"], ("failed", "cancelled"))

    def test_child_identity_persisted_before_delivery_bookkeeping(self):
        thread_id = "r3-identity-first"
        aid, mid, turn_id, prompt = self._schedule(thread_id)
        runner = daemon.get_runner(aid)
        real_reconcile = type(daemon.MAILBOX).reconcile_turn_delivery
        checked = {"done": False}

        def spy(*, turn_id: int, started: bool) -> int:
            # The first reconcile (started=True, immediately after identity
            # persistence) must already observe the child identity in the DB.
            if started and not checked["done"]:
                with daemon.connect() as db:
                    agent = db.execute("SELECT child_pid FROM agents WHERE id=?", (aid,)).fetchone()
                    turn = db.execute("SELECT child_started_at FROM turns WHERE id=?", (turn_id,)).fetchone()
                self.assertEqual(agent["child_pid"], 424242)
                self.assertIsNotNone(turn["child_started_at"])
                checked["done"] = True
            return real_reconcile(daemon.MAILBOX, turn_id=turn_id, started=started)

        with (
            mock.patch.object(daemon.MAILBOX, "reconcile_turn_delivery", new=spy),
            mock.patch.object(daemon.subprocess, "Popen", return_value=_FakeProc()),
            mock.patch.object(daemon, "process_create_time", return_value=12345.0),
        ):
            runner._run(turn_id, prompt)
        self.assertTrue(checked["done"], "reconcile spy never observed the persisted child identity")
        with daemon.connect() as db:
            agent = db.execute("SELECT child_pid,child_started_at FROM agents WHERE id=?", (aid,)).fetchone()
            turn = db.execute("SELECT child_started_at FROM turns WHERE id=?", (turn_id,)).fetchone()
        # PID marker is dropped post-run; the durable identity (child_started_at)
        # persists so recover() can still decide converge-vs-release by marker.
        self.assertIsNone(agent["child_pid"])
        self.assertEqual(agent["child_started_at"], "12345.0")
        self.assertEqual(turn["child_started_at"], "12345.0")

    def test_initial_delivery_mark_failure_eventually_reconciles(self):
        thread_id = "r3-mark-flaky"
        aid, mid, turn_id, prompt = self._schedule(thread_id)
        runner = daemon.get_runner(aid)
        real_reconcile = type(daemon.MAILBOX).reconcile_turn_delivery
        state = {"calls": 0}

        def flaky(*, turn_id: int, started: bool) -> int:
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("injected reconcile failure")
            return real_reconcile(daemon.MAILBOX, turn_id=turn_id, started=started)

        with (
            mock.patch.object(daemon.MAILBOX, "reconcile_turn_delivery", new=flaky),
            mock.patch.object(daemon.subprocess, "Popen", return_value=_FakeProc()),
            mock.patch.object(daemon, "process_create_time", return_value=12345.0),
        ):
            runner._run(turn_id, prompt)
        # The swallowed in-try failure must be converged by a later reconcile.
        self.assertGreaterEqual(state["calls"], 2)
        with daemon.connect() as db:
            msg = db.execute("SELECT state,consumed_at FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(msg["state"], "delivered")
        self.assertIsNotNone(msg["consumed_at"])

    def test_restart_reconciles_started_delivery_turn(self):
        thread_id = "r3-recover-started"
        aid, mid, turn_id, _prompt = self._schedule(thread_id)
        # Crash state: the child had started (marker durably set) and the turn
        # was left failed by a daemon restart mid-turn.
        with daemon.connect() as db:
            db.execute("UPDATE turns SET status='failed',child_started_at=? WHERE id=?", ("12345.0", turn_id))
            db.execute("UPDATE agents SET status='running' WHERE id=?", (aid,))
        daemon.recover(start_runners=False)
        with daemon.connect() as db:
            msg = db.execute("SELECT state,consumed_at FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(msg["state"], "delivered")
        self.assertIsNotNone(msg["consumed_at"])

    def test_failed_pre_spawn_restart_releases_claim(self):
        thread_id = "r3-recover-unstarted"
        aid, mid, turn_id, _prompt = self._schedule(thread_id)
        # Crash state: the child never spawned (no marker) and the turn was
        # left failed; its claim must be released on restart.
        with daemon.connect() as db:
            db.execute("UPDATE turns SET status='failed' WHERE id=?", (turn_id,))
            db.execute("UPDATE agents SET status='running' WHERE id=?", (aid,))
        daemon.recover(start_runners=False)
        with daemon.connect() as db:
            msg = db.execute("SELECT state,target_turn_id FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(msg["state"], "pending")
        self.assertIsNone(msg["target_turn_id"])

    def test_cancelled_agent_with_queued_turn_not_recovered(self):
        thread_id = "r3-recover-cancelled"
        cancelled_aid, _m1, _t1, _p1 = self._schedule(thread_id + "-a")
        with daemon.connect() as db:
            db.execute("UPDATE agents SET status='cancelled' WHERE id=?", (cancelled_aid,))
        live_aid, _m2, _t2, _p2 = self._schedule(thread_id + "-b")
        recorded: list[str] = []

        def recording_get_runner(agent_id: str):
            recorded.append(agent_id)
            return None

        with mock.patch.object(daemon, "get_runner", side_effect=recording_get_runner):
            daemon.recover_runners()
        self.assertNotIn(cancelled_aid, recorded)
        self.assertIn(live_aid, recorded)
        self.assertEqual(len(recorded), 1)

    def test_queued_turn_survives_restart(self):
        thread_id = "r3-queued-survives"
        aid, mid, turn_id, _prompt = self._schedule(thread_id)
        daemon.recover(start_runners=False)
        with daemon.connect() as db:
            turn = db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
            msg = db.execute("SELECT state,target_turn_id FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(turn["status"], "queued")
        self.assertEqual(msg["state"], "pending")
        self.assertEqual(msg["target_turn_id"], turn_id)

    def test_reconcile_turn_delivery_idempotent(self):
        thread_id = "r3-idempotent"
        _aid, mid, turn_id, _prompt = self._schedule(thread_id)
        mailbox = daemon.MAILBOX
        self.assertEqual(mailbox.reconcile_turn_delivery(turn_id=turn_id, started=True), 1)
        with daemon.connect() as db:
            first = db.execute(
                "SELECT state,delivered_at,consumed_at FROM agent_messages WHERE id=?", (mid,)
            ).fetchone()
        self.assertEqual(first["state"], "delivered")
        self.assertIsNotNone(first["delivered_at"])
        self.assertIsNotNone(first["consumed_at"])

        self.assertEqual(mailbox.reconcile_turn_delivery(turn_id=turn_id, started=True), 0)
        with daemon.connect() as db:
            second = db.execute(
                "SELECT state,delivered_at,consumed_at FROM agent_messages WHERE id=?", (mid,)
            ).fetchone()
        self.assertEqual(second["state"], "delivered")
        self.assertEqual(second["delivered_at"], first["delivered_at"])
        self.assertEqual(second["consumed_at"], first["consumed_at"])

        # started=False on already-delivered rows must never un-deliver them.
        self.assertEqual(mailbox.reconcile_turn_delivery(turn_id=turn_id, started=False), 0)
        with daemon.connect() as db:
            row = db.execute(
                "SELECT state,target_turn_id,delivered_at FROM agent_messages WHERE id=?", (mid,)
            ).fetchone()
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["target_turn_id"], turn_id)
        self.assertEqual(row["delivered_at"], first["delivered_at"])

    def test_reconcile_started_uses_coalesce(self):
        thread_id = "r3-coalesce"
        _aid, mid, turn_id, _prompt = self._schedule(thread_id)
        preset = "2026-01-01T00:00:00+00:00"
        with daemon.connect() as db:
            db.execute("UPDATE agent_messages SET delivered_at=? WHERE id=?", (preset, mid))
        self.assertEqual(daemon.MAILBOX.reconcile_turn_delivery(turn_id=turn_id, started=True), 1)
        with daemon.connect() as db:
            row = db.execute(
                "SELECT state,delivered_at,consumed_at FROM agent_messages WHERE id=?", (mid,)
            ).fetchone()
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["delivered_at"], preset)
        self.assertIsNotNone(row["consumed_at"])


if __name__ == "__main__":
    unittest.main()
