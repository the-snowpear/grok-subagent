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
import uuid
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


class RoleResolutionTests(OrchestrateV2Mixin, unittest.TestCase):
    """B1-B6: create-time role resolution over the profile machinery.

    A role is a create-time overlay: it fills the worktree default when the
    caller omitted ``worktree`` and appends a role-specific policy suffix to
    the effective prompt. Worktree precedence is: explicit ``worktree`` >
    explicit non-default ``profile`` > role ``worktree_default`` > default
    profile. Invalid roles raise ValueError before any durable state, and
    the batch-level role default is validated once up front. Read-only
    enforcement is prompt policy only (no OS/tool-level sandbox). All
    assertions are behavioral (DB rows, action responses, stored turn
    prompts, command builder).
    """

    def _turn_prompt(self, agent_id: str) -> str:
        with daemon.connect() as db:
            row = db.execute(
                "SELECT prompt FROM turns WHERE agent_id=? AND turn_no=1", (agent_id,)
            ).fetchone()
        self.assertIsNotNone(row, "first turn must exist with the effective prompt")
        return row["prompt"]

    def test_implement_role_defaults_to_worktree(self):
        """B1: role='implement' without worktree -> isolated worktree + policy suffix."""
        repo = self._make_git_repo(self.root / "repo")
        created = self._create("t-role-implement", name="impl", prompt="build it", cwd=str(repo), role="implement")
        aid = created["agent_id"]
        row = self._agent_row(aid)
        self.assertIsNotNone(row["worktree_root"], "implement role must default to an isolated worktree")
        self.assertIsNotNone(row["worktree_path"])
        self.assertEqual(row["isolation_mode"], "worktree")
        self.assertIn("[role: implement]", self._turn_prompt(aid))

        # An explicit worktree=False overrides the role default: shared cwd, no worktree dir.
        before = self._worktree_count()
        created2 = self._create(
            "t-role-implement-explicit", name="impl2", prompt="build it", cwd=str(repo),
            role="implement", worktree=False,
        )
        aid2 = created2["agent_id"]
        row2 = self._agent_row(aid2)
        self.assertIsNone(row2["worktree_root"], "explicit worktree=False must win over the implement default")
        self.assertIsNone(row2["worktree_path"])
        self.assertEqual(row2["isolation_mode"], "shared")
        self.assertEqual(Path(row2["cwd"]), repo)
        self.assertEqual(self._worktree_count(), before, "no worktree dir may be created for worktree=False")

    def test_explore_role_readonly_policy_and_shared_cwd(self):
        """B2: explore/review default to the shared cwd and carry the read-only policy."""
        for role in ("explore", "review"):
            created = self._create(f"t-role-{role}", name=role, prompt="investigate", role=role)
            aid = created["agent_id"]
            row = self._agent_row(aid)
            self.assertIsNone(row["worktree_root"], f"{role} role must default to the shared cwd")
            self.assertIsNone(row["worktree_path"])
            self.assertEqual(row["isolation_mode"], "shared")
            self.assertEqual(Path(row["cwd"]), self.root)
            prompt = self._turn_prompt(aid)
            self.assertIn(f"[role: {role}]", prompt)
            self.assertIn("只读", prompt, "read-only policy marker must be present")
            self.assertIn("禁止修改", prompt, "read-only policy marker must be present")

    def test_role_invalid_rejected(self):
        """B3: invalid roles raise ValueError before any durable or disk side effect."""
        thread_id = "t-role-invalid"
        repo = self._make_git_repo(self.root / "repo")
        before = self._worktree_count()
        for index, bad in enumerate(["hacker", "", 123]):
            with self.assertRaises(ValueError):
                daemon.action(
                    "create_agent",
                    {"agent_name": f"bad{index}", "prompt": "p", "cwd": str(repo), "role": bad},
                    {"codex_thread_id": thread_id, "codex_origin": "test"},
                )
        with daemon.connect() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
        self.assertEqual(count, 0, "no agent row may survive an invalid role")
        self.assertEqual(self._worktree_count(), before, "no worktree may be created for an invalid role")

    def test_role_batch_default_and_override(self):
        """B4: batch role default flows down; a per-item role wins over it."""
        thread_id = "t-role-batch"
        repo = self._make_git_repo(self.root / "repo")
        items = [
            {"agent_name": "a1", "prompt": "p1", "cwd": str(repo)},
            {"agent_name": "a2", "prompt": "p2", "cwd": str(repo), "role": "implement"},
        ]
        result = daemon.action(
            "create_agents",
            {"agents": items, "role": "explore"},
            {"codex_thread_id": thread_id, "codex_origin": "test"},
        )
        self.assertEqual(result["created"], 2)
        by_name = {item["index"]: item["agent_id"] for item in result["agents"]}
        row_a = self._agent_row(by_name[0])
        row_b = self._agent_row(by_name[1])
        self.assertIsNone(row_a["worktree_root"], "batch default explore -> shared cwd")
        self.assertIsNone(row_a["worktree_path"])
        self.assertEqual(row_a["isolation_mode"], "shared")
        self.assertIn("[role: explore]", self._turn_prompt(by_name[0]))
        self.assertIsNotNone(row_b["worktree_root"], "per-item implement must override the batch default")
        self.assertEqual(row_b["isolation_mode"], "worktree")
        self.assertIn("[role: implement]", self._turn_prompt(by_name[1]))

    def test_role_does_not_override_explicit_worktree(self):
        """B5: an explicit worktree=True wins over the explore role default."""
        repo = self._make_git_repo(self.root / "repo")
        created = self._create(
            "t-role-explicit", name="expl", prompt="inspect", cwd=str(repo),
            role="explore", worktree=True,
        )
        aid = created["agent_id"]
        row = self._agent_row(aid)
        self.assertIsNotNone(row["worktree_root"], "explicit worktree=True must win over the explore default")
        self.assertEqual(row["isolation_mode"], "worktree")
        self.assertIn("[role: explore]", self._turn_prompt(aid))

    def test_explicit_nondefault_profile_wins_over_role_default(self):
        """B7: profile='isolated' beats the explore/review shared-cwd defaults.

        An explicitly requested safety profile must never be downgraded by a
        role's worktree_default (regression: it used to become worktree=False).
        """
        repo = self._make_git_repo(self.root / "repo")
        for role in ("explore", "review"):
            created = self._create(
                f"t-role-profile-{role}", name=f"p-{role}", prompt="investigate",
                cwd=str(repo), profile="isolated", role=role,
            )
            row = self._agent_row(created["agent_id"])
            self.assertIsNotNone(row["worktree_root"], f"isolated profile must win over {role} default")
            self.assertIsNotNone(row["worktree_path"])
            self.assertEqual(row["isolation_mode"], "worktree")
            self.assertIn(f"[role: {role}]", self._turn_prompt(created["agent_id"]))

    def test_role_default_wins_over_default_profile(self):
        """B8: role='implement' default worktree beats the default profile."""
        repo = self._make_git_repo(self.root / "repo")
        created = self._create(
            "t-role-profile-impl", name="impl", prompt="build it", cwd=str(repo),
            role="implement",
        )
        row = self._agent_row(created["agent_id"])
        self.assertIsNotNone(row["worktree_root"], "implement role default must beat the default profile")
        self.assertEqual(row["isolation_mode"], "worktree")

    def test_explicit_worktree_false_wins_over_isolated_profile_and_role(self):
        """B9: explicit worktree=False beats profile='isolated' + role='implement'."""
        repo = self._make_git_repo(self.root / "repo")
        created = self._create(
            "t-role-profile-explicit", name="impl", prompt="build it", cwd=str(repo),
            profile="isolated", role="implement", worktree=False,
        )
        row = self._agent_row(created["agent_id"])
        self.assertIsNone(row["worktree_root"], "explicit worktree=False must win over everything")
        self.assertIsNone(row["worktree_path"])
        self.assertEqual(row["isolation_mode"], "shared")
        self.assertEqual(Path(row["cwd"]), repo)

    def test_flat_topology_unchanged(self):
        """B6: role plumbing must not change the flat grok_command topology."""
        created = self._create("t-role-flat", name="flat", prompt="p", role="review")
        aid = created["agent_id"]
        command = self._command(aid)
        self.assertIn("--no-subagents", command)
        self.assertIn("--always-approve", command)


class OrchestrationFlowTests(OrchestrateV2Mixin, unittest.TestCase):
    """C1-C2: thin-orchestrator control flow over the real runtime.

    Main (the test) is the ONLY decision maker; the runtime must transport
    the flow without making product decisions:
    Explorer evidence -> Main decision -> Implementer (isolated worktree +
    stored effort) -> fresh Reviewer -> Main-issued Fix Order via
    ``daemon.MAILBOX.send`` -> exactly one durable follow-up turn on the SAME
    agent row with the stored effort and session contract intact, the message
    claimed via ``target_turn_id`` and delivered+consumed after the follow-up
    runs, and a flat ``--no-subagents`` topology for every built command. A
    reviewer's finding alone must never mutate the runtime (no extra turns,
    no messages), proving the control plane requires Main to issue the Fix
    Order. All assertions are behavioral (DB rows, action responses, command
    builder output); no source-string inspection.
    """

    def test_thin_orchestrator_control_flow(self):
        thread_id = "t-thin-orchestrator"
        finding = "Fix Order: add input validation for empty payloads before the API call"

        # 1) Explorer: read-only policy role, shared cwd, effort omitted -> 'max'.
        explorer = self._create(
            thread_id,
            name="explorer",
            prompt="investigate the API surface and collect evidence for missing validation",
            role="explore",
        )
        explorer_aid = explorer["agent_id"]
        self._wait_terminal(explorer_aid)
        explorer_row = self._agent_row(explorer_aid)
        self.assertEqual(explorer_row["isolation_mode"], "shared")
        self.assertIsNone(explorer_row["worktree_root"])
        self.assertEqual(Path(explorer_row["cwd"]), self.root)
        explorer_result = daemon.action(
            "result", {"agent_id": explorer_aid}, {"codex_thread_id": thread_id}
        )
        self.assertEqual(
            explorer_result["reasoning_effort"], "max", "omitted effort must default to 'max'"
        )

        # 2) Main consumes the evidence and decides (the decision itself is
        #    simulated in test code; the runtime only has to carry the
        #    evidence). No semantic check on the payload.
        self.assertTrue(
            str(explorer_result["final_text"]).strip(),
            "explorer result must carry decision evidence (final_text non-empty)",
        )

        # 3) Implementer: worktree default -> isolated worktree, explicit 'high' effort.
        repo = self._make_git_repo(self.root / "repo")
        implementer = self._create(
            thread_id,
            name="implementer",
            prompt="implement the validated change",
            cwd=str(repo),
            role="implement",
            reasoning_effort="high",
        )
        impl_aid = implementer["agent_id"]
        self._wait_terminal(impl_aid)
        impl_row = self._agent_row(impl_aid)
        self.assertEqual(impl_row["isolation_mode"], "worktree")
        self.assertIsNotNone(impl_row["worktree_root"])
        self.assertIsNotNone(impl_row["worktree_path"])
        self.assertEqual(impl_row["reasoning_effort"], "high")
        impl_session = impl_row["grok_session_id"]
        impl_command = self._command(impl_aid)
        self.assertEqual(self._effort_arg(impl_command), "high", "command must carry the stored effort")
        self.assertIn("--no-subagents", impl_command)

        # 4) Fresh Reviewer: different agent id, shared cwd, explicit 'max' effort.
        reviewer = self._create(
            thread_id,
            name="reviewer",
            prompt="review the implementation and report findings only",
            role="review",
            reasoning_effort="max",
        )
        review_aid = reviewer["agent_id"]
        self._wait_terminal(review_aid)
        self.assertNotEqual(review_aid, impl_aid, "reviewer must be a fresh agent")
        review_row = self._agent_row(review_aid)
        self.assertEqual(review_row["isolation_mode"], "shared")
        self.assertIsNone(review_row["worktree_root"])
        self.assertEqual(Path(review_row["cwd"]), self.root)

        # 5) Main adjudicates (test code) and issues the Fix Order over the
        #    durable mailbox; the completed implementer must receive exactly
        #    one follow-up turn on the SAME agent row.
        daemon.MAILBOX.send(
            thread_id=thread_id,
            from_peer=main_peer_id(thread_id),
            to_peer=impl_aid,
            body=finding,
        )
        with daemon.connect() as db:
            message = db.execute(
                "SELECT id,state,consumed_at,target_turn_id FROM agent_messages "
                "WHERE to_peer=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (impl_aid,),
            ).fetchone()
            followups = [
                dict(row)
                for row in db.execute(
                    "SELECT id,agent_id,turn_no,prompt,status FROM turns WHERE agent_id=? AND turn_no=2",
                    (impl_aid,),
                )
            ]
            impl_after = dict(db.execute("SELECT * FROM agents WHERE id=?", (impl_aid,)).fetchone())
        self.assertIsNotNone(message, "the fix order must be a durable mailbox message")
        self.assertEqual(len(followups), 1, "exactly one follow-up turn (turn_no 2) may exist")
        followup = followups[0]
        self.assertEqual(followup["agent_id"], impl_aid, "follow-up must reuse the same agent row")
        self.assertEqual(followup["turn_no"], 2)
        self.assertEqual(
            message["target_turn_id"], followup["id"], "delivery must claim the message via target_turn_id"
        )
        self.assertIn(finding, followup["prompt"], "the follow-up prompt must carry the fix order body")
        self.assertEqual(impl_after["reasoning_effort"], "high", "scheduling must not re-resolve effort")
        self.assertEqual(impl_after["grok_session_id"], impl_session, "session contract must be unchanged")

        # 6) No duplicate follow-up; once the follow-up runs, the claimed
        #    message is delivered AND consumed.
        self._wait_terminal(impl_aid)
        with daemon.connect() as db:
            turn_count = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (impl_aid,)
            ).fetchone()["c"]
            message_after = db.execute(
                "SELECT state,consumed_at FROM agent_messages WHERE id=?", (message["id"],)
            ).fetchone()
            followup_after = db.execute(
                "SELECT status FROM turns WHERE agent_id=? AND turn_no=2", (impl_aid,)
            ).fetchone()
        self.assertEqual(turn_count, 2, "the implementer must have exactly turn 1 + one follow-up")
        self.assertEqual(message_after["state"], "delivered")
        self.assertIsNotNone(message_after["consumed_at"])
        self.assertEqual(followup_after["status"], "completed")

        # 7) Flat topology: every grok_command built for these agents is flat.
        for aid, label in ((explorer_aid, "explorer"), (impl_aid, "implementer"), (review_aid, "reviewer")):
            command = self._command(aid)
            self.assertIn("--no-subagents", command, f"{label} command must stay flat")
            self.assertIn("--always-approve", command)
        followup_command = self._command(impl_aid, "resume after fix order", first_turn=False)
        self.assertIn("--no-subagents", followup_command)
        self.assertIn("--resume", followup_command)
        self.assertEqual(self._effort_arg(followup_command), "high")

    def test_reviewer_finding_does_not_mutate_runtime(self):
        """C2: a completed reviewer leaves no turn/message residue behind.

        The reviewer's finding is produced, but the read-only role cannot
        authorize mutation: no follow-up turn, no queued work, and no
        messages from (or to) the reviewer peer. Only Main issuing a Fix
        Order mutates the runtime (covered by the control-flow test).
        """
        thread_id = "t-reviewer-readonly"
        reviewer = self._create(
            thread_id,
            name="reviewer",
            prompt="review the work and report findings; do not change anything",
            role="review",
        )
        review_aid = reviewer["agent_id"]
        self._wait_terminal(review_aid)
        result = daemon.action("result", {"agent_id": review_aid}, {"codex_thread_id": thread_id})
        self.assertTrue(str(result["final_text"]).strip(), "the reviewer finding must be produced")
        with daemon.connect() as db:
            turns = [
                dict(row)
                for row in db.execute(
                    "SELECT turn_no,status FROM turns WHERE agent_id=? ORDER BY turn_no", (review_aid,)
                )
            ]
            outgoing = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages WHERE from_peer=?", (review_aid,)
            ).fetchone()["c"]
            incoming = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages WHERE to_peer=?", (review_aid,)
            ).fetchone()["c"]
            queued = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=? AND status='queued'", (review_aid,)
            ).fetchone()["c"]
            status = db.execute("SELECT status FROM agents WHERE id=?", (review_aid,)).fetchone()["status"]
        self.assertEqual(
            turns, [{"turn_no": 1, "status": "completed"}], "a reviewer runs exactly one turn"
        )
        self.assertEqual(outgoing, 0, "a read-only reviewer must not send any message")
        self.assertEqual(incoming, 0, "no message may target the reviewer")
        self.assertEqual(queued, 0, "the reviewer must not leave queued work behind")
        self.assertEqual(status, "completed")


class RolePersistenceTests(OrchestrateV2Mixin, unittest.TestCase):
    """T1: role is durable on the agents row and surfaces through status/result.

    The stored role is the canonical scheduling identity: completed follow-ups
    reuse the same agent row, so the role is inherited without re-resolution.
    """

    def test_role_persists_and_surfaces_after_followup(self):
        """T1: create role='implement' -> DB, status/result, follow-up all keep it."""
        thread_id = "t-auth-role-persist"
        repo = self._make_git_repo(self.root / "repo")
        created = self._create(
            thread_id, name="impl", prompt="build", cwd=str(repo),
            role="implement", reasoning_effort="high",
        )
        aid = created["agent_id"]
        row = self._agent_row(aid)
        self.assertEqual(row["role"], "implement", "resolved role must be stored on the agents row")
        status = daemon.action("status", {"agent_id": aid}, {"codex_thread_id": thread_id})
        result = daemon.action("result", {"agent_id": aid}, {"codex_thread_id": thread_id})
        self.assertEqual(status["role"], "implement")
        self.assertEqual(result["role"], "implement")
        self.assertEqual(result["reasoning_effort"], "high")

        # A completed-worker follow-up reuses the same agent row: role retained.
        self._wait_terminal(aid)
        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=aid, body="fix order"
        )
        self._wait_terminal(aid)
        row_after = self._agent_row(aid)
        self.assertEqual(row_after["role"], "implement", "follow-up must inherit the stored role")
        self.assertEqual(row_after["reasoning_effort"], "high")

    def test_explore_role_persists(self):
        """T1: explore role is stored and surfaces in status."""
        thread_id = "t-auth-role-explore"
        created = self._create(thread_id, name="expl", prompt="investigate", role="explore")
        aid = created["agent_id"]
        self.assertEqual(self._agent_row(aid)["role"], "explore")
        status = daemon.action("status", {"agent_id": aid}, {"codex_thread_id": thread_id})
        self.assertEqual(status["role"], "explore")

    def test_legacy_role_null_keeps_legacy_auto_followup(self):
        """T2: a role NULL legacy agent keeps the historical auto-followup behavior."""
        thread_id = "t-auth-legacy"
        aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            role = db.execute("SELECT role FROM agents WHERE id=?", (aid,)).fetchone()["role"]
        self.assertIsNone(role, "legacy rows must stay role NULL")
        # A peer-origin message must still auto-wake a legacy agent: the
        # orchestrate authority gate only applies to role-tagged workers.
        daemon.MAILBOX.send(thread_id=thread_id, from_peer="peer-legacy", to_peer=aid, body="legacy wake")
        with daemon.connect() as db:
            msg = db.execute(
                "SELECT target_turn_id,state FROM agent_messages WHERE to_peer=?", (aid,)
            ).fetchone()
            turn = db.execute(
                "SELECT id,agent_id,turn_no FROM turns WHERE id=?", (msg["target_turn_id"],)
            ).fetchone()
        self.assertIsNotNone(msg["target_turn_id"], "legacy auto-followup must still claim the message")
        self.assertEqual(msg["state"], "pending", "claimed but not yet delivered (no child spawn)")
        self.assertEqual(turn["agent_id"], aid, "legacy follow-up reuses the same agent row")
        self.assertEqual(turn["turn_no"], 1, "seeded agent has no turn 1, so the first delivery is turn 1")


class MainOwnedFollowupAuthorityTests(OrchestrateV2Mixin, unittest.TestCase):
    """T3-T7: Main owns automatic follow-up scheduling for role-tagged workers.

    The runtime gate lives in maybe_schedule_delivery: a completed worker with
    a stored role (explore/implement/review) can only be auto-woken by a
    message whose sender is main_peer_id(thread_id). Peer messages keep the
    full mailbox semantics but never gain follow-up scheduling authority.
    """

    def _completed_implementer(self, thread_id: str) -> str:
        repo = self._make_git_repo(self.root / "repo")
        created = self._create(
            thread_id, name="impl", prompt="build the change", cwd=str(repo),
            role="implement", reasoning_effort="high",
        )
        aid = created["agent_id"]
        self._wait_terminal(aid)
        return aid

    def test_reviewer_peer_cannot_wake_completed_implementer(self):
        """T3: a reviewer finding alone must NOT create an implementer follow-up."""
        thread_id = "t-auth-t3"
        impl_aid = self._completed_implementer(thread_id)
        reviewer = self._create(thread_id, name="reviewer", prompt="review", role="review")
        review_aid = reviewer["agent_id"]
        self._wait_terminal(review_aid)

        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=review_aid, to_peer=impl_aid, body="please change X"
        )
        with daemon.connect() as db:
            msg = db.execute(
                "SELECT state,target_turn_id,consumed_at,delivered_at FROM agent_messages "
                "WHERE to_peer=? ORDER BY id DESC LIMIT 1",
                (impl_aid,),
            ).fetchone()
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (impl_aid,)
            ).fetchone()["c"]
            status = db.execute(
                "SELECT status FROM agents WHERE id=?", (impl_aid,)
            ).fetchone()["status"]
        self.assertEqual(msg["state"], "pending", "peer finding must stay pending")
        self.assertIsNone(msg["target_turn_id"], "peer finding must not be claimed")
        self.assertIsNone(msg["consumed_at"], "peer finding must not be consumed")
        self.assertIsNone(msg["delivered_at"], "peer finding must not be delivered")
        self.assertEqual(turns, 1, "reviewer finding must not create an implementer follow-up")
        self.assertEqual(status, "completed", "implementer must stay completed")

    def test_main_can_wake_same_implementer(self):
        """T4: Main's Fix Order wakes the same completed implementer exactly once."""
        thread_id = "t-auth-t4"
        impl_aid = self._completed_implementer(thread_id)
        before = self._agent_row(impl_aid)

        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=impl_aid,
            body="Fix Order: adjust input validation",
        )
        with daemon.connect() as db:
            msg = db.execute(
                "SELECT id,target_turn_id FROM agent_messages WHERE to_peer=? ORDER BY id DESC LIMIT 1",
                (impl_aid,),
            ).fetchone()
            followups = [
                dict(r)
                for r in db.execute(
                    "SELECT id,agent_id,turn_no FROM turns WHERE agent_id=? AND turn_no=2",
                    (impl_aid,),
                )
            ]
        self.assertEqual(len(followups), 1, "Main must create exactly one follow-up turn")
        followup = followups[0]
        self.assertEqual(msg["target_turn_id"], followup["id"], "Main message must claim the follow-up turn")
        self.assertEqual(followup["agent_id"], impl_aid, "follow-up must reuse the same agent row")
        self.assertEqual(followup["turn_no"], 2)

        # The follow-up executes; the same contract is retained and the Main
        # message is delivered + consumed (no duplicates).
        self._wait_terminal(impl_aid)
        after = self._agent_row(impl_aid)
        self.assertEqual(after["grok_session_id"], before["grok_session_id"])
        self.assertEqual(after["reasoning_effort"], before["reasoning_effort"])
        self.assertEqual(after["role"], before["role"])
        self.assertEqual(after["worktree_root"], before["worktree_root"])
        self.assertEqual(after["worktree_path"], before["worktree_path"])
        with daemon.connect() as db:
            turn_count = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (impl_aid,)
            ).fetchone()["c"]
            msg_after = db.execute(
                "SELECT state,consumed_at FROM agent_messages WHERE id=?", (msg["id"],)
            ).fetchone()
        self.assertEqual(turn_count, 2, "implementer must have exactly turn 1 + one follow-up")
        self.assertEqual(msg_after["state"], "delivered")
        self.assertIsNotNone(msg_after["consumed_at"])

    def test_mixed_queue_only_main_message_claimed(self):
        """T5: a peer message is never coalesced into a Main-authorized turn."""
        thread_id = "t-auth-t5"
        aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            db.execute("UPDATE agents SET role='implement' WHERE id=?", (aid,))
        daemon.MAILBOX.send(thread_id=thread_id, from_peer="reviewer-peer", to_peer=aid, body="finding")
        daemon.MAILBOX.send(thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=aid, body="fix order")
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT body,state,target_turn_id,consumed_at FROM agent_messages "
                "WHERE to_peer=? ORDER BY created_at,id",
                (aid,),
            ).fetchall()
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["c"]
        self.assertEqual(turns, 1, "only the Main message may create a follow-up turn")
        by_body = {r["body"]: r for r in rows}
        peer_msg = by_body["finding"]
        main_msg = by_body["fix order"]
        self.assertEqual(peer_msg["state"], "pending", "peer message must stay pending")
        self.assertIsNone(peer_msg["target_turn_id"], "peer message must not be claimed")
        self.assertIsNone(peer_msg["consumed_at"], "peer message must not be consumed")
        self.assertIsNotNone(main_msg["target_turn_id"], "Main message must be claimed")
        self.assertEqual(main_msg["state"], "pending", "claimed but not yet delivered (no child spawn)")

    def test_recovery_sweep_respects_authority_gate(self):
        """T6: recovery/sweep cannot wake a role-tagged worker from peer mail alone."""
        thread_id = "t-auth-t6"
        aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            db.execute("UPDATE agents SET role='implement' WHERE id=?", (aid,))
        daemon.MAILBOX.send(thread_id=thread_id, from_peer="reviewer-peer", to_peer=aid, body="finding")
        daemon.delivery_sweep()
        with daemon.connect() as db:
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["c"]
            msg = db.execute(
                "SELECT target_turn_id,state FROM agent_messages WHERE to_peer=?", (aid,)
            ).fetchone()
        self.assertEqual(turns, 0, "peer-only sweep must not create a follow-up")
        self.assertIsNone(msg["target_turn_id"])
        self.assertEqual(msg["state"], "pending")

        # A Main-authored pending message makes the same sweep able to wake it.
        daemon.MAILBOX.send(thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=aid, body="fix order")
        daemon.delivery_sweep()
        with daemon.connect() as db:
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["c"]
            main_msg = db.execute(
                "SELECT target_turn_id FROM agent_messages WHERE to_peer=? AND body='fix order'", (aid,)
            ).fetchone()
            peer_msg = db.execute(
                "SELECT target_turn_id FROM agent_messages WHERE to_peer=? AND body='finding'", (aid,)
            ).fetchone()
        self.assertEqual(turns, 1, "sweep must wake the worker once a Main message exists")
        self.assertIsNotNone(main_msg["target_turn_id"])
        self.assertIsNone(peer_msg["target_turn_id"], "peer message must still not be claimed")

    def test_main_can_wake_completed_reviewer_for_rereview(self):
        """T7: Main owns scheduling, so a Main request CAN wake a completed reviewer."""
        thread_id = "t-auth-t7"
        reviewer = self._create(thread_id, name="reviewer", prompt="review round 1", role="review")
        review_aid = reviewer["agent_id"]
        self._wait_terminal(review_aid)

        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=review_aid,
            body="verify fix round 1",
        )
        with daemon.connect() as db:
            followups = [
                dict(r)
                for r in db.execute(
                    "SELECT id,agent_id,turn_no FROM turns WHERE agent_id=? AND turn_no=2",
                    (review_aid,),
                )
            ]
            msg = db.execute(
                "SELECT target_turn_id FROM agent_messages WHERE to_peer=? ORDER BY id DESC LIMIT 1",
                (review_aid,),
            ).fetchone()
        self.assertEqual(len(followups), 1, "Main must be able to wake the reviewer for re-review")
        self.assertEqual(followups[0]["agent_id"], review_aid, "re-review must reuse the reviewer row")
        self.assertEqual(msg["target_turn_id"], followups[0]["id"])

    def test_main_message_not_starved_by_older_peer_messages(self):
        """T8: >100 older peer messages cannot starve a newer Main Fix Order.

        The authority sender filter must run inside SQL before the delivery
        batch LIMIT; otherwise the first 100 peer rows would consume the
        entire selection window and Main's later message would never be
        claimed, leaving the control plane unable to re-activate the worker.
        """
        thread_id = "t-auth-t8"
        aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            db.execute("UPDATE agents SET role='implement' WHERE id=?", (aid,))
        for index in range(125):
            daemon.MAILBOX.send(
                thread_id=thread_id, from_peer="reviewer-peer", to_peer=aid,
                body=f"peer-{index:03d}",
            )
        with daemon.connect() as db:
            peers = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages "
                "WHERE to_peer=? AND from_peer='reviewer-peer'",
                (aid,),
            ).fetchone()["c"]
            unclaimed = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages "
                "WHERE to_peer=? AND target_turn_id IS NULL",
                (aid,),
            ).fetchone()["c"]
        self.assertEqual(peers, 125)
        self.assertEqual(unclaimed, 125, "all peer messages must stay pending and unclaimed")

        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=aid,
            body="Fix Order: authoritative change",
        )
        with daemon.connect() as db:
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["c"]
            main_msg = db.execute(
                "SELECT target_turn_id FROM agent_messages "
                "WHERE to_peer=? AND from_peer=?",
                (aid, main_peer_id(thread_id)),
            ).fetchone()
            peer_rows = db.execute(
                "SELECT state,target_turn_id,consumed_at FROM agent_messages "
                "WHERE to_peer=? AND from_peer='reviewer-peer'",
                (aid,),
            ).fetchall()
            turn_prompt = db.execute(
                "SELECT prompt FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["prompt"]
        self.assertEqual(turns, 1, "Main must be scheduled despite 125 older peer messages")
        self.assertIsNotNone(main_msg["target_turn_id"], "Main message must be claimed")
        for row in peer_rows:
            self.assertEqual(row["state"], "pending", "no peer message may be claimed")
            self.assertIsNone(row["target_turn_id"])
            self.assertIsNone(row["consumed_at"])
        self.assertIn("Fix Order: authoritative change", turn_prompt)
        self.assertNotIn("peer-000", turn_prompt, "peer messages must not be coalesced into the turn")

    def test_delivery_sweep_not_starved_by_older_peer_messages(self):
        """T9: delivery_sweep must not be stuck behind the first 100 peer rows.

        With >100 older peer messages pending and no Main message, the sweep
        creates no follow-up. Once a Main message exists, the same sweep must
        claim it even though every row ahead of it in created_at order is an
        unauthorized peer message.
        """
        thread_id = "t-auth-t9"
        aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            db.execute("UPDATE agents SET role='implement' WHERE id=?", (aid,))
        stamp = daemon.now()
        with daemon.connect() as db:
            for index in range(125):
                db.execute(
                    "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,body,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), thread_id, "reviewer-peer", aid, f"peer-{index:03d}", stamp),
                )
        daemon.delivery_sweep()
        with daemon.connect() as db:
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["c"]
        self.assertEqual(turns, 0, "peer-only sweep must not create a follow-up")

        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,body,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), thread_id, main_peer_id(thread_id), aid,
                 "Fix Order: authoritative change", daemon.now()),
            )
        daemon.delivery_sweep()
        with daemon.connect() as db:
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["c"]
            main_msg = db.execute(
                "SELECT target_turn_id FROM agent_messages "
                "WHERE to_peer=? AND from_peer=?",
                (aid, main_peer_id(thread_id)),
            ).fetchone()
            unclaimed_peers = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages "
                "WHERE to_peer=? AND from_peer='reviewer-peer' AND target_turn_id IS NULL",
                (aid,),
            ).fetchone()["c"]
        self.assertEqual(turns, 1, "sweep must claim Main despite 125 older peer messages")
        self.assertIsNotNone(main_msg["target_turn_id"])
        self.assertEqual(unclaimed_peers, 125, "every peer message must stay unclaimed")

    def test_main_batch_limit_applies_after_authority_filter(self):
        """T10: LIMIT 100 applies to the Main-authorized window only.

        100 peer + 100 Main pending messages: the scheduler claims the whole
        Main batch in one follow-up while every peer message stays pending
        and unclaimed. Guards the boundary of the pre-LIMIT sender filter.
        """
        thread_id = "t-auth-t10"
        aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            db.execute("UPDATE agents SET role='implement' WHERE id=?", (aid,))
        stamp = daemon.now()
        with daemon.connect() as db:
            for index in range(100):
                db.execute(
                    "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,body,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), thread_id, "reviewer-peer", aid, f"peer-{index:03d}", stamp),
                )
            for index in range(100):
                db.execute(
                    "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,body,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), thread_id, main_peer_id(thread_id), aid,
                     f"main-{index:03d}", stamp),
                )
        with mock.patch.object(daemon, "get_runner", return_value=None):
            self.assertEqual(daemon.maybe_schedule_delivery(aid), 100)
        with daemon.connect() as db:
            turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (aid,)
            ).fetchone()["c"]
            claimed_main = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages "
                "WHERE to_peer=? AND from_peer=? AND target_turn_id IS NOT NULL",
                (aid, main_peer_id(thread_id)),
            ).fetchone()["c"]
            claimed_peers = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages "
                "WHERE to_peer=? AND from_peer='reviewer-peer' AND target_turn_id IS NOT NULL",
                (aid,),
            ).fetchone()["c"]
        self.assertEqual(turns, 1)
        self.assertEqual(claimed_main, 100, "all 100 Main messages must be claimed in one batch")
        self.assertEqual(claimed_peers, 0, "no peer message may be claimed")


class ReadOnlyRoleIsolationTests(OrchestrateV2Mixin, unittest.TestCase):
    """D1-D3: grok-work's recommended path isolates explore/review from the parent.

    The runtime stays generic (explicit worktree wins); the orchestration
    contract (SKILL.md / Work Order) is what requests worktree=True for
    git-backed explore/review. These tests prove that invocation path keeps
    the worker off the user's dirty working tree and leaves the parent
    untouched.
    """

    def test_git_backed_explore_and_review_run_isolated(self):
        thread_id = "t-isolation"
        repo = self._make_git_repo(self.root / "repo")
        (repo / "tracked.txt").write_text("base\nmodified\n", encoding="utf-8")
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        self.assertTrue(dirty, "the parent repo must be dirty for this test")
        parent_content_before = (repo / "tracked.txt").read_text(encoding="utf-8")
        worktree_base = (daemon.DATA / "worktrees").resolve()

        for role in ("explore", "review"):
            created = self._create(
                thread_id, name=role, prompt="investigate", cwd=str(repo), role=role, worktree=True,
            )
            aid = created["agent_id"]
            row = self._agent_row(aid)
            self.assertEqual(
                row["isolation_mode"], "worktree",
                f"{role} must run isolated on a git-backed orchestration task",
            )
            self.assertIsNotNone(row["worktree_root"])
            wt = Path(row["worktree_root"]).resolve()
            self.assertTrue(
                str(wt).startswith(str(worktree_base)),
                "worker worktree_root must live under DATA/worktrees",
            )
            worker_cwd = Path(row["cwd"]).resolve()
            self.assertNotEqual(worker_cwd, repo.resolve(), f"{role} must not stand in the original worktree")
            self.assertTrue(str(worker_cwd).startswith(str(wt)), f"{role} worker cwd must be inside its worktree")

        self.assertEqual(
            (repo / "tracked.txt").read_text(encoding="utf-8"),
            parent_content_before,
            "the dirty parent must stay untouched",
        )


class OrchestrationFollowUpE2ETests(OrchestrateV2Mixin, unittest.TestCase):
    """E1-E6: full authority boundary flow with a real isolated patch artifact.

    Explorer evidence -> Main decision -> Implementer (isolated worktree,
    effort high) -> materialized change -> real patch artifact -> Reviewer
    finding (NO runtime mutation on its own) -> Main Fix Order -> same
    Implementer follow-up -> Main re-review request -> same Reviewer
    follow-up. The reviewer re-review prompt references the REAL patch
    artifact path produced by the runtime, not a fictional string.
    """

    def test_main_owned_followup_flow_with_real_artifact(self):
        thread_id = "t-e2e-authority"
        repo = self._make_git_repo(self.root / "repo")

        # 1) Explorer: evidence for Main's decision (shared cwd by default).
        explorer = self._create(
            thread_id, name="explorer", prompt="collect evidence about validation gaps", role="explore",
        )
        explorer_aid = explorer["agent_id"]
        self._wait_terminal(explorer_aid)
        explorer_result = daemon.action("result", {"agent_id": explorer_aid}, {"codex_thread_id": thread_id})
        self.assertTrue(str(explorer_result["final_text"]).strip(), "explorer must return evidence")

        # 2) Main decides (test harness) and spawns an Implementer.
        implementer = self._create(
            thread_id, name="implementer", prompt="implement the validated change",
            cwd=str(repo), role="implement", reasoning_effort="high",
        )
        impl_aid = implementer["agent_id"]
        self._wait_terminal(impl_aid)
        impl_row = self._agent_row(impl_aid)
        self.assertEqual(impl_row["isolation_mode"], "worktree")
        self.assertEqual(impl_row["reasoning_effort"], "high")
        impl_session = impl_row["grok_session_id"]

        # 3) The harness stands in for the implementer's file work: write the
        #    change into the isolated worktree so the runtime produces a REAL
        #    patch artifact for the reviewer to inspect.
        impl_worktree = Path(impl_row["worktree_root"])
        (impl_worktree / "tracked.txt").write_text("base\nvalidated\n", encoding="utf-8")
        impl_result = daemon.action("result", {"agent_id": impl_aid}, {"codex_thread_id": thread_id})
        isolation = impl_result["isolation"]
        self.assertEqual(isolation["mode"], "worktree")
        patch_artifact = isolation.get("patch_artifact")
        self.assertIsNotNone(patch_artifact, "a real patch artifact must exist for the reviewer")
        artifact_abs = daemon.ROOT / patch_artifact
        self.assertTrue(artifact_abs.exists(), "the patch artifact must exist on disk")

        # 4) Fresh Reviewer (isolated per the orchestration contract).
        reviewer = self._create(
            thread_id, name="reviewer", prompt="review the implementation",
            cwd=str(repo), role="review", worktree=True,
        )
        review_aid = reviewer["agent_id"]
        self._wait_terminal(review_aid)
        self.assertNotEqual(review_aid, impl_aid)
        review_row = self._agent_row(review_aid)
        self.assertEqual(review_row["isolation_mode"], "worktree")

        # 5) Reviewer finding must NOT mutate the runtime: no implementer turn.
        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=review_aid, to_peer=impl_aid, body="finding: validation is missing",
        )
        with daemon.connect() as db:
            impl_turns = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (impl_aid,)
            ).fetchone()["c"]
        self.assertEqual(impl_turns, 1, "reviewer finding alone must not create an implementer turn")

        # 6) Main Fix Order wakes the SAME implementer; the real artifact is
        #    referenced in the follow-up evidence.
        fix_order = (
            "Fix Order: add validation for empty payloads. "
            f"Patch artifact to verify against: {patch_artifact}"
        )
        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=impl_aid, body=fix_order,
        )
        self._wait_terminal(impl_aid)
        impl_after = self._agent_row(impl_aid)
        self.assertEqual(impl_after["grok_session_id"], impl_session)
        self.assertEqual(impl_after["reasoning_effort"], "high")
        self.assertEqual(impl_after["role"], "implement")
        self.assertEqual(impl_after["worktree_root"], impl_row["worktree_root"])
        with daemon.connect() as db:
            impl_turn_count = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=?", (impl_aid,)
            ).fetchone()["c"]
        self.assertEqual(impl_turn_count, 2, "implementer must have exactly turn 1 + one follow-up")

        # 7) Main wakes the SAME Reviewer for re-review, referencing the real
        #    patch artifact path; the reviewer follow-up completes.
        rereview = (
            "verify fix round 1 against the patch artifact: "
            f"{artifact_abs} (sha256 {isolation.get('patch_sha256')})"
        )
        daemon.MAILBOX.send(
            thread_id=thread_id, from_peer=main_peer_id(thread_id), to_peer=review_aid, body=rereview,
        )
        self._wait_terminal(review_aid)
        with daemon.connect() as db:
            review_turns = [
                dict(r)
                for r in db.execute(
                    "SELECT turn_no,status FROM turns WHERE agent_id=? ORDER BY turn_no", (review_aid,)
                )
            ]
            reviewer_msgs = db.execute(
                "SELECT COUNT(*) AS c FROM agent_messages WHERE to_peer=?", (review_aid,)
            ).fetchone()["c"]
        self.assertEqual(
            review_turns,
            [{"turn_no": 1, "status": "completed"}, {"turn_no": 2, "status": "completed"}],
            "Main must be able to wake the reviewer for a re-review follow-up",
        )
        self.assertEqual(reviewer_msgs, 1, "exactly one re-review message, no duplicates")

        # 8) Flat topology: every built command stays --no-subagents.
        for aid, label in ((explorer_aid, "explorer"), (impl_aid, "implementer"), (review_aid, "reviewer")):
            command = self._command(aid)
            self.assertIn("--no-subagents", command, f"{label} command must stay flat")
        impl_followup_command = self._command(impl_aid, "resume", first_turn=False)
        self.assertIn("--no-subagents", impl_followup_command)
        self.assertIn("--resume", impl_followup_command)
        self.assertEqual(self._effort_arg(impl_followup_command), "high")


if __name__ == "__main__":
    unittest.main()
