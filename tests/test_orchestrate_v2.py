"""Behavioral regression tests for the reasoning-effort control plane (commit 1).

Contract under test (daemon.py):
- ``reasoning_effort`` is a canonical control-plane field with shape-only
  validation (no invented enum): ``REASONING_EFFORT_DEFAULT`` = "max", the
  regex ``^[A-Za-z][A-Za-z0-9_-]{0,31}$`` rejects empty/whitespace/non-str/
  malformed values with ValueError, and the resolved value is persisted on
  the agents row at create time.
- Precedence: per-agent explicit > batch/workflow override > runtime default.
- ``create_agent`` / ``create_agents`` accept the optional field; an omitted
  value persists "max". Invalid values raise ValueError with no durable or
  worktree ghost state (validation happens before any worktree creation or
  DB insert).
- ``grok_command`` appends the VERIFIED ``--reasoning-effort <value>`` flag
  (after ``--max-turns``) using the STORED row value, falling back to the
  runtime default only for rows that do not carry the field. Follow-up turns
  reuse the same agent row (delivery claim via ``target_turn_id``), so the
  stored effort is inherited without re-resolution.
- ``status`` / ``result`` responses expose ``reasoning_effort``.

Every assertion is behavioral (DB rows, action responses, command builder
output). No source-string inspection. Faults are injected by patching daemon
module-level symbols / environment, exactly like the Round 2/4 suites.
"""

from __future__ import annotations

import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

import daemon
from coordination import main_peer_id
from tests.test_round2_review_fixes import Round2Mixin


ROOT = Path(__file__).resolve().parents[1]
FAKE_GROK = ROOT / "tests" / "fake_grok.py"

INVALID_EFFORTS = ["", " ", "bad value!", 123, "x" * 40]


class OrchestrateV2Mixin(Round2Mixin):
    """Round 4 fixture style: temp ROOT/DATA/DB + init_db + seed helpers.

    Adds the deterministic fake-grok environment and raised concurrency
    limits so real ``action("create_agent")`` calls run end to end.
    """

    def setUp(self):
        super().setUp()
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        self._orig_ensure_viewer = daemon.ensure_viewer
        daemon.ensure_viewer = lambda agent_id=None: False
        self._prev_no_browser = os.environ.get("GROK_OBSERVER_NO_BROWSER")
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        self._orig_limits = {
            "MAX_ACTIVE_PER_THREAD": daemon.MAX_ACTIVE_PER_THREAD,
            "MAX_ACTIVE_AGENTS": daemon.MAX_ACTIVE_AGENTS,
        }
        daemon.MAX_ACTIVE_PER_THREAD = 20
        daemon.MAX_ACTIVE_AGENTS = 100
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
        daemon.ensure_viewer = self._orig_ensure_viewer
        if self._prev_no_browser is None:
            os.environ.pop("GROK_OBSERVER_NO_BROWSER", None)
        else:
            os.environ["GROK_OBSERVER_NO_BROWSER"] = self._prev_no_browser
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        super().tearDown()

    # -- helpers -----------------------------------------------------------

    def _create(self, thread_id: str, name: str = "w", prompt: str = "do work", **extra) -> dict:
        args = {"agent_name": name, "prompt": prompt, "cwd": str(self.root)}
        args.update(extra)
        return daemon.action("create_agent", args, {"codex_thread_id": thread_id, "codex_origin": "test"})

    def _agent_row(self, agent_id: str) -> dict:
        with daemon.connect() as db:
            return dict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())

    def _command(self, agent_id: str, prompt: str = "p", first_turn: bool = True) -> list[str]:
        return daemon.grok_command(self._agent_row(agent_id), prompt, first_turn, self.root)

    def _effort_arg(self, command: list[str]) -> str:
        self.assertIn("--reasoning-effort", command)
        return command[command.index("--reasoning-effort") + 1]

    def _assert_effort_command(self, command: list[str], expected: str) -> None:
        """B3 pre-check rides along: flat topology must stay intact."""
        self.assertEqual(self._effort_arg(command), expected)
        self.assertIn("--no-subagents", command)
        self.assertIn("--always-approve", command)

    def _wait_terminal(self, agent_id: str, timeout: float = 10.0) -> None:
        """Block until the agent reaches a terminal status (fake Grok is fast)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with daemon.connect() as db:
                status = db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
            if status is None or status["status"] not in ("queued", "running"):
                return
            time.sleep(0.02)
        self.fail(f"agent {agent_id} did not reach a terminal status within {timeout}s")

    def _make_git_repo(self, folder: Path) -> Path:
        """Initialize a git repository with one committed file."""
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=folder, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=folder, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=folder, check=True, capture_output=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=folder, check=True, capture_output=True)
        (folder / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=folder, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=folder, check=True, capture_output=True)
        return folder

    def _worktree_count(self) -> int:
        root = daemon.DATA / "worktrees"
        return len(list(root.glob("*"))) if root.exists() else 0

    def _status_result_effort(self, agent_id: str, thread_id: str) -> tuple[str, str]:
        status = daemon.action("status", {"agent_id": agent_id}, {"codex_thread_id": thread_id})
        result = daemon.action("result", {"agent_id": agent_id}, {"codex_thread_id": thread_id})
        return status["reasoning_effort"], result["reasoning_effort"]


class EffortControlPlaneTests(OrchestrateV2Mixin, unittest.TestCase):
    """A1-A7: validation, precedence, persistence, plumbing, inheritance."""

    def test_effort_default_max(self):
        """A1: omitted effort persists 'max' and shows up in status/result/command."""
        thread_id = "t-effort-default"
        created = self._create(thread_id, name="w1")
        aid = created["agent_id"]
        self.assertEqual(self._agent_row(aid)["reasoning_effort"], "max")
        status_effort, result_effort = self._status_result_effort(aid, thread_id)
        self.assertEqual(status_effort, "max")
        self.assertEqual(result_effort, "max")
        self._assert_effort_command(self._command(aid), "max")

    def test_effort_explicit_override(self):
        """A2: an explicit value wins over the default and is never reverted."""
        thread_id = "t-effort-explicit"
        created = self._create(thread_id, name="w2", reasoning_effort="high")
        aid = created["agent_id"]
        row = self._agent_row(aid)
        self.assertEqual(row["reasoning_effort"], "high")
        status_effort, result_effort = self._status_result_effort(aid, thread_id)
        self.assertEqual(status_effort, "high")
        self.assertEqual(result_effort, "high")
        command = self._command(aid)
        self.assertEqual(self._effort_arg(command), "high")
        self.assertNotEqual(row["reasoning_effort"], daemon.REASONING_EFFORT_DEFAULT)

    def test_effort_invalid_leaves_no_ghost_state(self):
        """A3: malformed efforts raise ValueError before ANY durable or disk side effect."""
        thread_id = "t-effort-invalid"
        repo = self._make_git_repo(self.root / "repo")
        before = self._worktree_count()
        for index, value in enumerate(INVALID_EFFORTS):
            with self.assertRaises(ValueError):
                daemon.action(
                    "create_agent",
                    {
                        "agent_name": f"bad{index}",
                        "prompt": "p",
                        "cwd": str(repo),
                        "profile": "deep",
                        "reasoning_effort": value,
                    },
                    {"codex_thread_id": thread_id, "codex_origin": "test"},
                )
        with daemon.connect() as db:
            counts = (
                db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"],
                db.execute("SELECT COUNT(*) AS c FROM turns").fetchone()["c"],
                db.execute("SELECT COUNT(*) AS c FROM search_index").fetchone()["c"],
            )
        self.assertEqual(counts, (0, 0, 0), "no row of any kind may survive an invalid effort")
        self.assertEqual(self._worktree_count(), before, "no worktree may be created for an invalid effort")

    def test_effort_batch_default(self):
        """A4: a batch-level default flows down to every item without an override."""
        thread_id = "t-effort-batch-default"
        items = [
            {"agent_name": "a1", "prompt": "p1", "cwd": str(self.root)},
            {"agent_name": "a2", "prompt": "p2", "cwd": str(self.root)},
        ]
        result = daemon.action(
            "create_agents",
            {"agents": items, "reasoning_effort": "low"},
            {"codex_thread_id": thread_id, "codex_origin": "test"},
        )
        self.assertEqual(result["created"], 2)
        for item in result["agents"]:
            self.assertEqual(self._agent_row(item["agent_id"])["reasoning_effort"], "low")

    def test_effort_batch_per_item_override(self):
        """A5: a per-item effort wins over the batch default."""
        thread_id = "t-effort-batch-override"
        items = [
            {"agent_name": "a1", "prompt": "p1", "cwd": str(self.root)},
            {"agent_name": "a2", "prompt": "p2", "cwd": str(self.root), "reasoning_effort": "high"},
        ]
        result = daemon.action(
            "create_agents",
            {"agents": items, "reasoning_effort": "low"},
            {"codex_thread_id": thread_id, "codex_origin": "test"},
        )
        self.assertEqual(result["created"], 2)
        by_name = {item["index"]: item["agent_id"] for item in result["agents"]}
        self.assertEqual(self._agent_row(by_name[0])["reasoning_effort"], "low")
        self.assertEqual(self._agent_row(by_name[1])["reasoning_effort"], "high")

    def test_effort_followup_preserves_stored_value(self):
        """A6: follow-up turns inherit the stored effort, never re-resolved.

        Proven at the command-builder level (the full fake-grok follow-up is
        a scheduling detail): with REASONING_EFFORT_DEFAULT patched to 'high'
        AFTER creation, the stored 'low' row still emits 'low', a row that
        carries no stored effort (legacy-shaped SELECT; fresh insert with the
        column default) emits the patched default, and the mailbox delivery
        path reuses the same agent row (target_turn_id claim) so no
        re-resolution path exists.
        """
        thread_id = "t-effort-followup"
        created = self._create(thread_id, name="w6", reasoning_effort="low")
        aid = created["agent_id"]
        self._wait_terminal(aid)

        with mock.patch.object(daemon, "REASONING_EFFORT_DEFAULT", "high"):
            # 1) The stored row keeps winning after the runtime default changes.
            row = self._agent_row(aid)
            self.assertEqual(row["reasoning_effort"], "low")
            self._assert_effort_command(self._command(aid, "follow up", first_turn=False), "low")

            # 2) A fresh insert without the field gets the SQL column default
            #    ('max', independent of the patched constant) and a row dict
            #    whose SELECT shape predates the field falls back to the
            #    (patched) runtime default in grok_command.
            fresh = self.seed_agent("t-effort-fallback", "completed")
            with daemon.connect() as db:
                stored = db.execute("SELECT reasoning_effort FROM agents WHERE id=?", (fresh,)).fetchone()
            self.assertEqual(stored["reasoning_effort"], "max")
            with daemon.connect() as db:
                legacy = dict(
                    db.execute("SELECT id,grok_session_id,max_turns FROM agents WHERE id=?", (fresh,)).fetchone()
                )
            self._assert_effort_command(daemon.grok_command(legacy, "x", True, self.root), "high")

            # 3) Delivery scheduling claims the message onto a turn of the SAME
            #    agent row; the stored effort is untouched and inherited.
            daemon.MAILBOX.send(
                thread_id=thread_id,
                from_peer=main_peer_id(thread_id),
                to_peer=aid,
                body="follow-up request",
            )
            with daemon.connect() as db:
                message = db.execute(
                    "SELECT target_turn_id FROM agent_messages WHERE to_peer=? ORDER BY id DESC LIMIT 1",
                    (aid,),
                ).fetchone()
                turn = db.execute(
                    "SELECT id,agent_id,turn_no FROM turns WHERE id=?",
                    (message["target_turn_id"],),
                ).fetchone()
                agent = dict(db.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone())
            self.assertIsNotNone(message["target_turn_id"], "delivery must claim the message")
            self.assertEqual(turn["agent_id"], aid, "follow-up reuses the same agent row")
            self.assertEqual(turn["turn_no"], 2)
            self.assertEqual(agent["reasoning_effort"], "low", "scheduling must not re-resolve effort")
            self._assert_effort_command(
                daemon.grok_command(agent, "delivered follow-up", False, self.root), "low"
            )

    def test_effort_backward_compatible(self):
        """A7: a pre-commit caller that never sends the field gets the default."""
        thread_id = "t-effort-backward"
        created = self._create(thread_id, name="legacy")
        aid = created["agent_id"]
        self.assertEqual(self._agent_row(aid)["reasoning_effort"], "max")
        status_effort, result_effort = self._status_result_effort(aid, thread_id)
        self.assertEqual(status_effort, "max")
        self.assertEqual(result_effort, "max")
        self._assert_effort_command(self._command(aid), "max")


if __name__ == "__main__":
    unittest.main()
