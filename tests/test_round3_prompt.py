"""Behavioral regression tests for Round 3, commit 3 (prompt transport + delivery sweep).

Contract under test (prompt_transport.py + daemon.py):
- Small prompts travel in argv; large prompts never travel in full in argv on
  the Windows UTF-16 code-unit policy (windows_argv_limit=True makes the policy
  testable on any OS; POSIX always uses argv).
- Large prompts are written durably to data/prompts/<agent_id>/<turn_id>.txt
  and delivered either through a probed native --prompt-file flag or through a
  short wrapper prompt pointing at the file (capability is probed, never
  assumed; probe is cached once per process and falls back to the wrapper).
- Post-commit delivery scheduling failures are logged (stderr + event) and
  retried by a periodic sweep that needs no daemon restart and stays
  idempotent (one follow-up turn per agent at most).
- Recovered runners start only after the worker ports are bound:
  recover(start_runners=False) performs no runner work; recover_runners() does.

Every assertion is behavioral (transport fields, file contents, DB state,
events). No source-string inspection.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import daemon
import prompt_transport
from coordination import main_peer_id
from tests.test_round2_review_fixes import Round2Mixin


class Round3Mixin(Round2Mixin):
    """Round3 fixture: Round2 paths plus a sandboxed prompts dir."""

    def setUp(self):
        super().setUp()
        self.prompts_dir = self.root / "prompts"

    def prepare_transport(self, agent_id: str, turn_id: int, prompt: str, **kwargs) -> prompt_transport.PromptTransport:
        kwargs.setdefault("prompts_dir", self.prompts_dir)
        return prompt_transport.prepare_prompt_transport(agent_id, turn_id, prompt, **kwargs)

    def count_turns(self, agent_id: str) -> int:
        with daemon.connect() as db:
            return int(
                db.execute("SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (agent_id,)).fetchone()["c"]
            )


class PromptTransportTests(Round3Mixin, unittest.TestCase):
    def test_small_prompt_keeps_argv_path(self):
        transport = self.prepare_transport("a", 1, "short prompt")
        self.assertEqual(transport.mode, "argv")
        self.assertEqual(transport.argv_prompt, "short prompt")
        self.assertIsNone(transport.prompt_file)
        self.assertIsNone(transport.prompt_file_flag)

    def test_large_ascii_prompt_uses_file_transport(self):
        prompt = "A" * 60000
        transport = self.prepare_transport(
            "a", 2, prompt, prompt_file_support="--prompt-file", windows_argv_limit=True
        )
        self.assertEqual(transport.mode, "prompt_file")
        self.assertIsNotNone(transport.prompt_file)
        target = Path(transport.prompt_file)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), prompt)
        # Exactly one prompt source: no argv/wrapper prompt, only the native flag.
        self.assertIsNone(transport.argv_prompt)
        self.assertEqual(transport.prompt_file_flag, "--prompt-file")

    def test_large_ascii_prompt_full_content_not_in_argv(self):
        prompt = "A" * 60000
        transport = self.prepare_transport("a", 3, prompt, prompt_file_support=None, windows_argv_limit=True)
        self.assertEqual(transport.mode, "wrapper_file")
        self.assertIsNone(transport.prompt_file_flag)
        agent_row = {"grok_session_id": "sess-1", "max_turns": 50}
        command = daemon.grok_command(
            agent_row,
            transport.argv_prompt or "wrapper",
            True,
            Path("."),
            prompt_file_flag=transport.prompt_file_flag,
            prompt_file=transport.prompt_file,
        )
        self.assertNotIn(prompt, command)
        self.assertEqual(Path(transport.prompt_file).read_text(encoding="utf-8"), prompt)

    def test_wrapper_fallback_uses_short_argv_and_durable_file(self):
        prompt = "B" * 60000
        transport = self.prepare_transport("a", 4, prompt, prompt_file_support=None, windows_argv_limit=True)
        self.assertEqual(transport.mode, "wrapper_file")
        self.assertIsNotNone(transport.argv_prompt)
        self.assertIn("authoritative task", transport.argv_prompt)
        self.assertLess(len(transport.argv_prompt), 1000)
        self.assertNotIn(prompt, transport.argv_prompt)
        self.assertEqual(Path(transport.prompt_file).read_text(encoding="utf-8"), prompt)
        self.assertIsNone(transport.prompt_file_flag)


class PromptCapabilityProbeTests(Round3Mixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        prompt_transport._PROBE_CACHE = prompt_transport._UNSET

    def tearDown(self):
        prompt_transport._PROBE_CACHE = prompt_transport._UNSET
        super().tearDown()

    def test_prompt_file_capability_probe_supported(self):
        fake = subprocess.CompletedProcess(
            args=["grok", "-p", "--help"],
            returncode=0,
            stdout="usage: grok -p PROMPT [options]\n  --prompt-file FILE  read the prompt from FILE\n",
        )
        with mock.patch.object(prompt_transport.subprocess, "run", return_value=fake) as run:
            self.assertEqual(prompt_transport.probe_prompt_file_support(), "--prompt-file")
            # Module-level cache: the probe runs once per process (a repeated
            # probe must not re-invoke the CLI).
            first_count = run.call_count
            self.assertEqual(prompt_transport.probe_prompt_file_support(), "--prompt-file")
        self.assertEqual(run.call_count, first_count)

    def test_prompt_file_capability_probe_unsupported(self):
        fake = subprocess.CompletedProcess(
            args=["grok", "-p", "--help"],
            returncode=0,
            stdout="usage: grok -p PROMPT [options]\n",
        )
        with mock.patch.object(prompt_transport.subprocess, "run", return_value=fake):
            self.assertIsNone(prompt_transport.probe_prompt_file_support())
        # Probe failures (missing binary, subprocess errors) also mean
        # unsupported; reset the cache so the error path actually runs.
        prompt_transport._PROBE_CACHE = prompt_transport._UNSET
        with mock.patch.object(prompt_transport.subprocess, "run", side_effect=OSError("no grok on PATH")):
            self.assertIsNone(prompt_transport.probe_prompt_file_support())


class DeliverySweepTests(Round3Mixin, unittest.TestCase):
    def test_post_commit_scheduler_failure_is_logged_and_retried(self):
        thread_id = "r3-sweep-retry"
        aid = self.seed_agent(thread_id, "completed")
        mid = self.seed_message(thread_id, aid, "retry me")

        class _FakeRunner:
            def __init__(self):
                self.enqueue_calls = 0

            def enqueue(self, turn_id: int, prompt: str) -> None:
                self.enqueue_calls += 1
                if self.enqueue_calls == 1:
                    raise ValueError("injected enqueue failure")

        fake_runner = _FakeRunner()
        with mock.patch.object(daemon, "get_runner", return_value=fake_runner):
            daemon.MAILBOX.send(
                thread_id=thread_id,
                from_peer=main_peer_id(thread_id),
                to_peer=aid,
                body="retry me",
            )
        with daemon.connect() as db:
            msg = db.execute("SELECT state,target_turn_id FROM agent_messages WHERE id=?", (mid,)).fetchone()
            turn = db.execute("SELECT id,status FROM turns WHERE agent_id=?", (aid,)).fetchone()
        # The send itself succeeded and the durable scheduling happened; only the
        # wake (enqueue) failed, which is logged as an event, not swallowed.
        self.assertIsNotNone(msg)
        self.assertEqual(msg["state"], "pending")
        self.assertIsNotNone(msg["target_turn_id"])
        self.assertIsNotNone(turn)
        self.assertEqual(turn["status"], "queued")
        self.assertEqual(self.count_turns(aid), 1)
        with daemon.connect() as db:
            ev = db.execute(
                "SELECT type FROM events WHERE agent_id=? AND type='delivery_wake_error'", (aid,)
            ).fetchone()
        self.assertIsNotNone(ev, "wake failure must be logged as delivery_wake_error")

        # The sweep retries without a daemon restart and stays idempotent.
        daemon.delivery_sweep()
        self.assertEqual(self.count_turns(aid), 1)
        daemon.delivery_sweep()
        self.assertEqual(self.count_turns(aid), 1)
        with daemon.connect() as db:
            claimed = db.execute("SELECT target_turn_id FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(claimed["target_turn_id"], msg["target_turn_id"])

        # Sweep-side scheduling errors are logged (delivery_sweep_error) and do
        # not propagate; the durable message stays pending for a later sweep.
        aid2 = self.seed_agent(thread_id + "-b", "completed")
        mid2 = self.seed_message(thread_id + "-b", aid2, "pending for sweep")
        with mock.patch.object(daemon, "maybe_schedule_delivery", side_effect=RuntimeError("sweep injection")):
            daemon.delivery_sweep()
        with daemon.connect() as db:
            ev2 = db.execute(
                "SELECT type FROM events WHERE agent_id=? AND type='delivery_sweep_error'", (aid2,)
            ).fetchone()
            msg2 = db.execute("SELECT state,target_turn_id FROM agent_messages WHERE id=?", (mid2,)).fetchone()
        self.assertIsNotNone(ev2, "sweep failure must be logged as delivery_sweep_error")
        self.assertEqual(msg2["state"], "pending")
        self.assertIsNone(msg2["target_turn_id"])
        self.assertEqual(self.count_turns(aid2), 0)

    def test_delivery_sweep_is_idempotent(self):
        thread_id = "r3-sweep-idem"
        aid = self.seed_agent(thread_id, "completed")
        mid = self.seed_message(thread_id, aid, "one-shot")
        with mock.patch.object(daemon, "get_runner", return_value=None):
            self.assertEqual(daemon.maybe_schedule_delivery(aid), 1)
        with daemon.connect() as db:
            turn = db.execute("SELECT id,status FROM turns WHERE agent_id=?", (aid,)).fetchone()
            # Agent back to completed: the sweep WILL consider it, yet the
            # existing queued turn must suppress any second delivery.
            db.execute("UPDATE agents SET status='completed' WHERE id=?", (aid,))
        # An additional unclaimed message must not create a second turn either:
        # the active queued turn guard wins even though the sweep selects the
        # agent again.
        unclaimed_mid = self.seed_message(thread_id, aid, "still unclaimed")
        self.assertIsNotNone(turn)
        self.assertEqual(turn["status"], "queued")
        daemon.delivery_sweep()
        self.assertEqual(self.count_turns(aid), 1)
        with daemon.connect() as db:
            msg = db.execute("SELECT state,target_turn_id FROM agent_messages WHERE id=?", (mid,)).fetchone()
            unclaimed = db.execute(
                "SELECT state,target_turn_id FROM agent_messages WHERE id=?", (unclaimed_mid,)
            ).fetchone()
        self.assertEqual(msg["state"], "pending")
        self.assertEqual(msg["target_turn_id"], turn["id"])
        # The unclaimed message is preserved for a later sweep, not lost.
        self.assertEqual(unclaimed["state"], "pending")
        self.assertIsNone(unclaimed["target_turn_id"])

    def test_sweep_skips_running_and_failed(self):
        for status in ("running", "failed"):
            thread_id = "r3-sweep-" + status
            aid = self.seed_agent(thread_id, status)
            self.seed_message(thread_id, aid, "unscheduled")
        daemon.delivery_sweep()
        with daemon.connect() as db:
            turns = int(db.execute("SELECT COUNT(*) AS c FROM turns").fetchone()["c"])
            messages = db.execute("SELECT state,target_turn_id FROM agent_messages").fetchall()
        self.assertEqual(turns, 0, "sweep must only schedule for completed agents")
        for message in messages:
            self.assertEqual(message["state"], "pending")
            self.assertIsNone(message["target_turn_id"])

    def test_recovered_runners_start_only_after_port_bind_step(self):
        thread_id = "r3-port-order"
        aid = self.seed_agent(thread_id, "completed")
        self.seed_message(thread_id, aid, "queued delivery")
        with mock.patch.object(daemon, "get_runner", return_value=None):
            self.assertEqual(daemon.maybe_schedule_delivery(aid), 1)
        recorded: list[str] = []

        def recording_get_runner(agent_id: str, *, create: bool = True):
            recorded.append(agent_id)
            return None

        # recover(start_runners=False) is the pre-bind phase: no runner work.
        with mock.patch.object(daemon, "get_runner", side_effect=recording_get_runner):
            daemon.recover(start_runners=False)
        self.assertEqual(recorded, [], "recover(start_runners=False) must not touch runners")
        # recover_runners() is the post-bind phase: exactly the queued agent.
        with mock.patch.object(daemon, "get_runner", side_effect=recording_get_runner):
            daemon.recover_runners()
        self.assertEqual(recorded, [aid])


if __name__ == "__main__":
    unittest.main()
