"""Behavioral regression tests for delivery-marker separation (Round 4, commit 1).

Contract under test (daemon.py):
- P1-A: the delivery-start proof is turns.child_spawned_at, written in the SAME
  transaction as agents.child_pid immediately after Popen. The OS process
  identity (agents.child_started_at) is a separate best-effort write AFTER the
  marker, so a slow/None process_create_time lookup can never delay or drop
  the delivery marker. turns.child_started_at is legacy and never written.
- P1-B: recover() kills a recorded child pid ONLY with positive identity
  evidence: pid alive, both expected (agents.child_started_at) and actual
  (process_create_time) creation times present, parseable, and within 2.0s.
  Missing/malformed/mismatched identity falls through to NOT killed.
- init_db backfills turns.child_spawned_at from the legacy
  turns.child_started_at marker for round-3 rows and stays idempotent.

Every assertion is behavioral (DB state, kill calls through a recorder,
message visibility through the mailbox). No source-string inspection. Faults
are injected by patching daemon module-level symbols / AgentRunner methods.
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


class Round4Mixin(Round2Mixin):
    """Round 4 fixture style: temp ROOT/DATA/DB + init_db + scheduling helpers."""

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

    def _seed_running_agent(self, thread_id: str, expected: str | None) -> str:
        """Schedule a delivery turn, then put the agent in crash state with a live-looking pid.

        The turn stays queued so recover() skips it; only the kill logic runs.
        Returns the agent id.
        """
        aid, _mid, _turn, _prompt = self._schedule(thread_id)
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET status='running',child_pid=?,child_started_at=? WHERE id=?",
                (424242, expected, aid),
            )
        return aid

    def _assert_not_killed(self, aid: str, killed: list[int]) -> None:
        """Identity evidence was insufficient: no kill and the error says so."""
        self.assertEqual(killed, [])
        with daemon.connect() as db:
            row = db.execute("SELECT error FROM agents WHERE id=?", (aid,)).fetchone()
        self.assertIn("could not be identity-verified, not killed", row["error"])
        self.assertIn("424242", row["error"])


class SpawnMarkerRegressionTests(Round4Mixin, unittest.TestCase):
    def test_process_create_time_none_still_marks_turn_spawned(self):
        thread_id = "r4-marker-none"
        aid, mid, turn_id, prompt = self._schedule(thread_id)
        runner = daemon.get_runner(aid)
        real_reconcile = type(daemon.MAILBOX).reconcile_turn_delivery
        checked = {"done": False}

        def spy(*, turn_id: int, started: bool) -> int:
            # First started reconcile: the delivery marker (child_pid +
            # turns.child_spawned_at) must already be durable even though the
            # OS create-time lookup returned None.
            if started and not checked["done"]:
                with daemon.connect() as db:
                    agent = db.execute("SELECT child_pid,child_started_at FROM agents WHERE id=?", (aid,)).fetchone()
                    turn = db.execute("SELECT child_spawned_at FROM turns WHERE id=?", (turn_id,)).fetchone()
                self.assertEqual(agent["child_pid"], 424242)
                self.assertIsNotNone(turn["child_spawned_at"])
                self.assertIsNone(agent["child_started_at"])
                checked["done"] = True
            return real_reconcile(daemon.MAILBOX, turn_id=turn_id, started=started)

        with (
            mock.patch.object(daemon.MAILBOX, "reconcile_turn_delivery", new=spy),
            mock.patch.object(daemon.subprocess, "Popen", return_value=_FakeProc()),
            mock.patch.object(daemon, "probe_prompt_file_support", return_value=None),
            mock.patch.object(daemon, "process_create_time", return_value=None),
        ):
            runner._run(turn_id, prompt)
        self.assertTrue(checked["done"], "reconcile spy never observed the persisted delivery marker")
        with daemon.connect() as db:
            agent = db.execute("SELECT child_pid,child_started_at FROM agents WHERE id=?", (aid,)).fetchone()
            turn = db.execute("SELECT child_spawned_at,child_started_at FROM turns WHERE id=?", (turn_id,)).fetchone()
        # No identity was ever recorded (lookup returned None), but the turn is
        # still durably marked as spawned; the legacy column stays untouched.
        self.assertIsNone(agent["child_pid"])
        self.assertIsNone(agent["child_started_at"])
        self.assertIsNotNone(turn["child_spawned_at"])
        self.assertIsNone(turn["child_started_at"])
        with daemon.connect() as db:
            msg = db.execute("SELECT state FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(msg["state"], "delivered")

    def test_process_create_time_none_delivery_reconciles_after_restart(self):
        thread_id = "r4-restart-none"
        aid, mid, turn_id, _prompt = self._schedule(thread_id)
        # Crash state: the child spawned (marker durably set) but the OS
        # identity was never recorded (process_create_time returned None) and
        # the daemon died mid-turn.
        with daemon.connect() as db:
            db.execute("UPDATE turns SET status='failed',child_spawned_at=? WHERE id=?", ("12345.0", turn_id))
            db.execute("UPDATE agents SET status='running' WHERE id=?", (aid,))
        daemon.recover(start_runners=False)
        with daemon.connect() as db:
            msg = db.execute("SELECT state,consumed_at FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(msg["state"], "delivered")
        self.assertIsNotNone(msg["consumed_at"])

    def test_recover_does_not_kill_pid_when_expected_create_time_missing(self):
        thread_id = "r4-kill-no-expected"
        aid = self._seed_running_agent(thread_id, expected=None)
        killed: list[int] = []
        with (
            mock.patch.object(daemon, "pid_is_alive", return_value=True),
            mock.patch.object(daemon, "process_create_time", return_value=12345.0),
            mock.patch.object(daemon, "terminate_pid", side_effect=killed.append),
        ):
            daemon.recover(start_runners=False)
        self._assert_not_killed(aid, killed)

    def test_recover_does_not_kill_pid_when_actual_create_time_missing(self):
        thread_id = "r4-kill-no-actual"
        aid = self._seed_running_agent(thread_id, expected="12345.0")
        killed: list[int] = []
        with (
            mock.patch.object(daemon, "pid_is_alive", return_value=True),
            mock.patch.object(daemon, "process_create_time", return_value=None),
            mock.patch.object(daemon, "terminate_pid", side_effect=killed.append),
        ):
            daemon.recover(start_runners=False)
        self._assert_not_killed(aid, killed)

    def test_recover_does_not_kill_pid_when_create_time_malformed(self):
        # Expected value unparseable: falls through to not killed.
        thread_id = "r4-kill-bad-expected"
        aid = self._seed_running_agent(thread_id, expected="not-a-number")
        killed: list[int] = []
        with (
            mock.patch.object(daemon, "pid_is_alive", return_value=True),
            mock.patch.object(daemon, "process_create_time", return_value=12345.0),
            mock.patch.object(daemon, "terminate_pid", side_effect=killed.append),
        ):
            daemon.recover(start_runners=False)
        self._assert_not_killed(aid, killed)

        # Actual lookup raising: recovery aborts rather than killing an
        # unverified pid.
        thread_id2 = "r4-kill-bad-actual"
        aid2 = self._seed_running_agent(thread_id2, expected="12345.0")
        killed2: list[int] = []
        with (
            mock.patch.object(daemon, "pid_is_alive", return_value=True),
            mock.patch.object(daemon, "process_create_time", side_effect=ValueError("injected lookup failure")),
            mock.patch.object(daemon, "terminate_pid", side_effect=killed2.append),
        ):
            with self.assertRaises(ValueError):
                daemon.recover(start_runners=False)
        self.assertEqual(killed2, [])

    def test_recover_kills_pid_only_when_identity_matches(self):
        thread_id = "r4-kill-match"
        aid = self._seed_running_agent(thread_id, expected="12345.0")
        killed: list[int] = []
        with (
            mock.patch.object(daemon, "pid_is_alive", return_value=True),
            mock.patch.object(daemon, "process_create_time", return_value=12345.5),
            mock.patch.object(daemon, "terminate_pid", side_effect=killed.append),
        ):
            daemon.recover(start_runners=False)
        self.assertEqual(killed, [424242])
        with daemon.connect() as db:
            row = db.execute("SELECT error FROM agents WHERE id=?", (aid,)).fetchone()
        self.assertIn("orphan process reaped (pid=424242)", row["error"])

    def test_recover_skips_reused_pid_on_mismatch(self):
        thread_id = "r4-kill-reuse"
        aid = self._seed_running_agent(thread_id, expected="12345.0")
        killed: list[int] = []
        with (
            mock.patch.object(daemon, "pid_is_alive", return_value=True),
            mock.patch.object(daemon, "process_create_time", return_value=99999.0),
            mock.patch.object(daemon, "terminate_pid", side_effect=killed.append),
        ):
            daemon.recover(start_runners=False)
        self._assert_not_killed(aid, killed)

    def test_migration_backfills_child_spawned_at_from_old_marker(self):
        thread_id = "r4-backfill"
        aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at,child_started_at) "
                "VALUES(?,?,?,?,?,?)",
                (aid, 1, "old prompt", "failed", daemon.now(), "12345.0"),
            )
            turn_id = int(db.execute("SELECT id FROM turns WHERE agent_id=?", (aid,)).fetchone()["id"])
            before = db.execute(
                "SELECT child_spawned_at,child_started_at FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
        self.assertIsNone(before["child_spawned_at"])
        self.assertEqual(before["child_started_at"], "12345.0")
        # Re-running init_db must be idempotent (guarded ALTERs) and must
        # backfill the round-3 delivery marker into the new column.
        daemon.init_db()
        with daemon.connect() as db:
            after = db.execute(
                "SELECT child_spawned_at,child_started_at FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
        self.assertEqual(after["child_spawned_at"], "12345.0")
        self.assertEqual(after["child_started_at"], "12345.0")


if __name__ == "__main__":
    unittest.main()
