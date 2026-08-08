"""Behavioral regression tests for Round 4, commit 2 (prompt transport retention).

Contract under test (prompt_transport.py + daemon.py):
- P1-C: Windows prompt sizing counts UTF-16 code units, not code points.
  Astral-plane prompts (emoji) that fit 20k code points but exceed 20k UTF-16
  units must use the file transport.
- P2-A: EXACTLY ONE prompt source per transport mode. prompt_file mode carries
  argv_prompt=None plus only the native --prompt-file flag; wrapper_file mode
  carries a short wrapper prompt and NO native flag.
- P2-B: the capability probe is lazy. Short prompts never invoke the CLI;
  oversized prompts probe exactly once.
- P2-C: durable prompt files under data/prompts/<agent_id> are removed only by
  the daemon (manual agent deletion and retention cleanup) and survive normal
  turn completion.

Every assertion is behavioral (transport fields, file contents, command argv,
DB state). No source-string inspection. The probe is always patched or pinned
(explicit prompt_file_support / windows_argv_limit) wherever a file transport
is exercised, so no real subprocess ever runs and no probe-cache warmth is
relied on.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import daemon
import prompt_transport
from tests.test_round2_review_fixes import Round2Mixin


class _FakeStream:
    """Empty stdout/stderr stand-in: iteration terminates, close is a no-op."""

    def __iter__(self):
        return iter(())

    def close(self) -> None:
        pass


class _FakeProc:
    """Controlled subprocess.Popen stand-in for runner _run drives.

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


class Round4PromptMixin(Round2Mixin):
    """Round4 prompt fixture: sandboxed data/prompts + transport/scheduling helpers."""

    def setUp(self):
        super().setUp()
        # Mirrors the production layout: daemon.DATA/"prompts" is where the
        # daemon retains prompt files and removes them on deletion/cleanup.
        self.prompts_dir = daemon.DATA / "prompts"
        prompt_transport._PROBE_CACHE = prompt_transport._UNSET

    def tearDown(self):
        prompt_transport._PROBE_CACHE = prompt_transport._UNSET
        super().tearDown()

    def prepare_transport(self, agent_id: str, turn_id: int, prompt: str, **kwargs) -> prompt_transport.PromptTransport:
        kwargs.setdefault("prompts_dir", self.prompts_dir)
        return prompt_transport.prepare_prompt_transport(agent_id, turn_id, prompt, **kwargs)

    def seed_prompt_file(self, agent_id: str, turn_id: int, content: str = "full prompt") -> Path:
        target = self.prompts_dir / agent_id / f"{turn_id}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return target

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


class UTF16CodeUnitsTests(unittest.TestCase):
    def test_utf16_code_units_ascii(self):
        self.assertEqual(prompt_transport.utf16_code_units("A"), 1)

    def test_utf16_code_units_bmp(self):
        self.assertEqual(prompt_transport.utf16_code_units("大"), 1)

    def test_utf16_code_units_astral(self):
        self.assertEqual(prompt_transport.utf16_code_units("😀"), 2)


class PromptTransportSizingTests(Round4PromptMixin, unittest.TestCase):
    def test_emoji_prompt_over_utf16_budget_uses_file_transport(self):
        # 17000 emoji = 17000 code points but 34000 UTF-16 units: the old
        # code-point policy would have kept this in argv; the UTF-16 policy
        # must route it to the durable file.
        prompt = "😀" * 17000
        transport = self.prepare_transport(
            "a", 1, prompt, prompt_file_support="--prompt-file", windows_argv_limit=True
        )
        self.assertEqual(transport.mode, "prompt_file")
        self.assertIsNone(transport.argv_prompt)
        self.assertEqual(transport.prompt_file_flag, "--prompt-file")
        self.assertIsNotNone(transport.prompt_file)
        self.assertEqual(Path(transport.prompt_file).read_text(encoding="utf-8"), prompt)
        agent_row = {"grok_session_id": "sess-1", "max_turns": 50}
        command = daemon.grok_command(
            agent_row,
            transport.argv_prompt,
            True,
            Path("."),
            prompt_file_flag=transport.prompt_file_flag,
            prompt_file=transport.prompt_file,
        )
        self.assertNotIn(prompt, command)

    def test_large_ascii_prompt_retained_regression(self):
        prompt = "A" * 60000
        transport = self.prepare_transport("a", 2, prompt, prompt_file_support=None, windows_argv_limit=True)
        self.assertEqual(transport.mode, "wrapper_file")
        self.assertEqual(Path(transport.prompt_file).read_text(encoding="utf-8"), prompt)
        self.assertIsNotNone(transport.argv_prompt)
        self.assertLess(len(transport.argv_prompt), 1000)
        self.assertIsNone(transport.prompt_file_flag)

    def test_prompt_file_mode_has_no_wrapper_prompt(self):
        prompt = "C" * 60000
        transport = self.prepare_transport(
            "a", 3, prompt, prompt_file_support="--prompt-file", windows_argv_limit=True
        )
        self.assertEqual(transport.mode, "prompt_file")
        self.assertIsNone(transport.argv_prompt, "prompt_file mode must carry exactly one prompt source")
        agent_row = {"grok_session_id": "sess-1", "max_turns": 50}
        command = daemon.grok_command(
            agent_row,
            transport.argv_prompt,
            True,
            Path("."),
            prompt_file_flag=transport.prompt_file_flag,
            prompt_file=transport.prompt_file,
        )
        self.assertEqual(transport.prompt_file_flag, "--prompt-file")
        self.assertNotIn("authoritative task", " ".join(command))

    def test_wrapper_mode_has_short_prompt_and_no_native_flag(self):
        prompt = "D" * 60000
        transport = self.prepare_transport("a", 4, prompt, prompt_file_support=None, windows_argv_limit=True)
        self.assertEqual(transport.mode, "wrapper_file")
        self.assertIn("authoritative task", transport.argv_prompt)
        self.assertIsNone(transport.prompt_file_flag)
        agent_row = {"grok_session_id": "sess-1", "max_turns": 50}
        command = daemon.grok_command(
            agent_row,
            transport.argv_prompt,
            True,
            Path("."),
            prompt_file_flag=transport.prompt_file_flag,
            prompt_file=transport.prompt_file,
        )
        self.assertNotIn("--prompt-file", command)
        self.assertNotIn(prompt, command)


class LazyProbeTests(Round4PromptMixin, unittest.TestCase):
    def test_short_prompt_does_not_probe_cli(self):
        calls = {"probe": 0, "run": 0}

        def exploding_probe():
            calls["probe"] += 1
            raise AssertionError("short prompts must never probe the CLI")

        def recording_run(*args, **kwargs):
            calls["run"] += 1
            raise AssertionError("no subprocess may run for a short prompt")

        with (
            mock.patch.object(prompt_transport, "probe_prompt_file_support", side_effect=exploding_probe),
            mock.patch.object(prompt_transport.subprocess, "run", side_effect=recording_run),
        ):
            transport = self.prepare_transport("a", 1, "short prompt", windows_argv_limit=True)
        self.assertEqual(transport.mode, "argv")
        self.assertEqual(transport.argv_prompt, "short prompt")
        self.assertIsNone(transport.prompt_file)
        self.assertIsNone(transport.prompt_file_flag)
        self.assertEqual(calls, {"probe": 0, "run": 0})

    def test_oversized_prompt_probes_lazily(self):
        probe = mock.Mock(return_value="--prompt-file")
        with mock.patch.object(prompt_transport, "probe_prompt_file_support", probe):
            transport = self.prepare_transport("a", 2, "A" * 60000, windows_argv_limit=True)
        self.assertEqual(transport.mode, "prompt_file")
        self.assertEqual(probe.call_count, 1, "oversized prompts must probe exactly once")
        self.assertEqual(transport.prompt_file_flag, "--prompt-file")
        self.assertIsNone(transport.argv_prompt)
        self.assertEqual(Path(transport.prompt_file).read_text(encoding="utf-8"), "A" * 60000)


class PromptFileRetentionTests(Round4PromptMixin, unittest.TestCase):
    def test_manual_delete_removes_agent_prompt_dir(self):
        thread_id = "r4-delete-prompts"
        aid = self.seed_agent(thread_id, "completed")
        self.seed_prompt_file(aid, 1)
        art = daemon.ARTIFACTS / aid
        art.mkdir(parents=True, exist_ok=True)
        (art / "y.txt").write_text("y", encoding="utf-8")

        server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/agents/{aid}/delete",
                method="POST",
                data=b"",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read().decode())
            self.assertTrue(payload["deleted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        with daemon.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM agents WHERE id=?", (aid,)).fetchone())
        self.assertFalse((self.prompts_dir / aid).exists(), "manual delete must remove the agent prompt dir")
        self.assertFalse(art.exists())

    def test_cleanup_removes_agent_prompt_dir(self):
        thread_id = "r4-cleanup-prompts"
        aid = self.seed_agent(thread_id, "completed")
        self.seed_prompt_file(aid, 1)
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with daemon.connect() as db:
            db.execute("UPDATE agents SET updated_at=? WHERE id=?", (old, aid))
        with mock.patch.object(daemon, "RETENTION_DAYS", 0):
            daemon.cleanup()
        with daemon.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM agents WHERE id=?", (aid,)).fetchone())
        self.assertFalse((self.prompts_dir / aid).exists(), "retention cleanup must remove the agent prompt dir")

    def test_turn_completion_keeps_prompt_file(self):
        thread_id = "r4-retain"
        aid, _mid, turn_id, _prompt = self._schedule(thread_id)
        runner = daemon.get_runner(aid)
        big_prompt = "A" * 60000
        real_prepare = prompt_transport.prepare_prompt_transport

        def pinned_prepare(agent_id: str, turn_id_arg: int, prompt: str):
            # Real prepare path, pinned to the Windows code-unit policy and the
            # sandboxed prompts dir so the test is deterministic on any OS.
            return real_prepare(
                agent_id, turn_id_arg, prompt, windows_argv_limit=True, prompts_dir=self.prompts_dir
            )

        with (
            mock.patch.object(daemon, "prepare_prompt_transport", side_effect=pinned_prepare),
            mock.patch.object(prompt_transport, "probe_prompt_file_support", return_value=None),
            mock.patch.object(daemon.subprocess, "Popen", return_value=_FakeProc()),
            mock.patch.object(daemon, "process_create_time", return_value=None),
        ):
            runner._run(turn_id, big_prompt)
        target = self.prompts_dir / aid / f"{turn_id}.txt"
        self.assertTrue(target.exists(), "prompt file must survive normal turn completion")
        self.assertEqual(target.read_text(encoding="utf-8"), big_prompt)
        with daemon.connect() as db:
            turn = db.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
        self.assertEqual(turn["status"], "completed")


if __name__ == "__main__":
    unittest.main()
