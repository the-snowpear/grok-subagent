"""Behavioral regression tests for the V2.1 orchestrate patch.

Covers the Main-approved V2.1 contracts end to end, behavior-level only
(DB rows, action responses, command builder output, server tool dispatch):
- ``result`` compact (default, no prompts/history, bounded multi-turn size)
  vs ``full`` (legacy turn_results/changes preserved);
- every Main action is cross-thread denied while the owning thread succeeds;
- signoff freshness gates plus clear-on-turn when new work is scheduled;
- mailbox ``after_message_id`` cursor liveness and foreign-cursor isolation;
- >8192-byte Main-facing message bodies externalized (preview/size/sha256/
  artifact_ref) while the DB row and worker-facing surfaces stay lossless;
- structured artifact refs (absolute+relative path / sha256 / size / encoding)
  for patch and untracked files, including post-worktree-deletion replay;
- batch worktree default / per-item override / legacy fallback, batch
  validation/operational/internal partial envelopes (created ids stay
  visible, processing stops safely on internal failure);
- MCP ``result`` compact JSON text while ``structuredContent`` stays identical;
- Linux /proc zombie (state Z) simulation and unreadable-/proc fallback;
- real re-review chain: artifact A -> Main-authorized fix -> updated
  result/artifact B -> Reviewer request references B, not A.

Faults are injected by patching daemon module-level symbols / environment,
exactly like the Round 2/3/4 suites. No source-string inspection.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import locale
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import daemon
import native_transport
import prompt_transport
import server as mcp_server
from coordination import main_peer_id

ROOT = Path(__file__).resolve().parents[1]
FAKE_GROK = ROOT / "tests" / "fake_grok.py"


class V21Mixin:
    """Self-contained fixture: temp daemon paths, fresh DB, fake grok, git helpers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = {
            "ROOT": daemon.ROOT,
            "DATA": daemon.DATA,
            "ARTIFACTS": daemon.ARTIFACTS,
            "DB_PATH": daemon.DB_PATH,
            "STATE_PATH": daemon.STATE_PATH,
            "LOCK_PATH": daemon.LOCK_PATH,
        }
        daemon.ROOT = self.root
        daemon.DATA = self.root / "data"
        daemon.ARTIFACTS = daemon.DATA / "artifacts"
        daemon.DB_PATH = daemon.DATA / "observer.sqlite"
        daemon.STATE_PATH = daemon.DATA / "daemon-state.json"
        daemon.LOCK_PATH = daemon.DATA / "daemon.lock"
        daemon.DATA.mkdir(parents=True, exist_ok=True)
        daemon.ARTIFACTS.mkdir(parents=True, exist_ok=True)
        daemon.init_db()
        with daemon.RUNNERS_LOCK:
            for runner in list(daemon.RUNNERS.values()):
                runner.shutdown()
            daemon.RUNNERS.clear()
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
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
        with daemon.RUNNERS_LOCK:
            for runner in list(daemon.RUNNERS.values()):
                runner.shutdown()
            daemon.RUNNERS.clear()
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
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        for key, value in self.saved.items():
            setattr(daemon, key, value)
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _create(self, thread_id: str, name: str, prompt: str = "do work", cwd: Path | None = None, **extra) -> dict:
        args = {"agent_name": name, "prompt": prompt, "cwd": str(cwd or self.root)}
        args.update(extra)
        return daemon.action("create_agent", args, {"codex_thread_id": thread_id, "codex_origin": "test"})

    def _agent_row(self, agent_id: str) -> dict:
        with daemon.connect() as db:
            return dict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())

    def _wait_terminal(self, agent_id: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with daemon.connect() as db:
                status = db.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
            if status is None or status["status"] not in ("queued", "running"):
                return
            time.sleep(0.02)
        self.fail(f"agent {agent_id} did not reach a terminal status within {timeout}s")

    def make_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "v21@example.invalid"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "V21"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True, capture_output=True)
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        return repo


class ResultCompactTests(V21Mixin, unittest.TestCase):
    """Compact/default vs full result; no prompts/history; bounded multi-turn size."""

    def test_compact_default_vs_full_and_invalid_detail(self):
        thread = "t-compact"
        aid = self._create(thread, "compact worker")["agent_id"]
        self._wait_terminal(aid)

        compact = daemon.action("result", {"agent_id": aid}, {"codex_thread_id": thread})
        self.assertEqual(compact["kind"], "agent_result")
        self.assertIn("turn_summary", compact)
        self.assertNotIn("turn_results", compact)
        self.assertNotIn("changes", compact)
        for entry in compact["turn_summary"]:
            self.assertNotIn("prompt", entry, "compact must not expose turn prompts")
            self.assertNotIn("result", entry, "compact must not expose turn result bodies")
            self.assertTrue({"turn_no", "status", "stop_reason", "created_at", "started_at", "completed_at"}.issuperset(entry.keys()))

        # Omitted detail == compact (default), not full.
        default = daemon.action("result", {"agent_id": aid}, {"codex_thread_id": thread})
        self.assertIn("turn_summary", default)
        self.assertNotIn("turn_results", default)

        full = daemon.action("result", {"agent_id": aid, "detail": "full"}, {"codex_thread_id": thread})
        self.assertIn("turn_results", full)
        self.assertIn("changes", full)
        self.assertNotIn("turn_summary", full)
        self.assertTrue(full["turn_results"][0]["prompt"].startswith("do work"))
        self.assertIn("[Agent Fabric coordination]", full["turn_results"][0]["prompt"])

        with self.assertRaises(ValueError):
            daemon.action("result", {"agent_id": aid, "detail": "verbose"}, {"codex_thread_id": thread})

    def test_compact_bounded_multi_turn_no_prompt_history(self):
        thread = "t-bounded"
        aid = self._create(thread, "bounded worker")["agent_id"]
        self._wait_terminal(aid)
        marker = "SECRET-PROMPT-MARKER"
        for i in range(3):
            big = f"{marker}-{i}-" + "x" * 20000
            daemon.action("send", {"agent_id": aid, "prompt": big}, {"codex_thread_id": thread})
            self._wait_terminal(aid)

        compact = daemon.action("result", {"agent_id": aid}, {"codex_thread_id": thread})
        self.assertEqual(compact["turns"], 4)
        text = json.dumps(compact, ensure_ascii=False)
        self.assertLess(len(text.encode("utf-8")), 4096, "compact result must stay bounded")
        self.assertNotIn(marker, text, "compact must not carry prompt bodies")
        for entry in compact["turn_summary"]:
            self.assertNotIn("prompt", entry)

        full = daemon.action("result", {"agent_id": aid, "detail": "full"}, {"codex_thread_id": thread})
        self.assertTrue(
            any(marker in t["prompt"] for t in full["turn_results"]),
            "full keeps the legacy prompt history",
        )


class CrossThreadAndSignoffTests(V21Mixin, unittest.TestCase):
    """Every Main action is cross-thread denied; signoff freshness + clear-on-turn."""

    def test_cross_thread_denied_same_thread_ok_and_signoff_clear_on_turn(self):
        thread_a = "t-owner"
        thread_b = "t-other"
        aid = self._create(thread_a, "owner worker")["agent_id"]
        self._wait_terminal(aid)
        foreign = {"codex_thread_id": thread_b}
        own = {"codex_thread_id": thread_a}

        for action, args in [
            ("status", {"agent_id": aid}),
            ("result", {"agent_id": aid}),
            ("send", {"agent_id": aid, "prompt": "steal"}),
            ("update_agent", {"agent_id": aid, "prompt": "steal"}),
            ("cancel", {"agent_id": aid}),
            ("signoff", {"agent_id": aid, "verdict": "rejected"}),
            ("wait", {"agent_id": aid, "timeout_seconds": 1}),
        ]:
            with self.assertRaises(ValueError) as ctx:
                daemon.action(action, args, foreign)
            self.assertIn("agent not found", str(ctx.exception), action)

        # Same thread succeeds for status/result.
        self.assertEqual(daemon.action("status", {"agent_id": aid}, own)["status"], "completed")
        self.assertIn("turn_summary", daemon.action("result", {"agent_id": aid}, own))

        # Signoff freshness: accepted only for completed; partial/rejected ok.
        accepted = daemon.action("signoff", {"agent_id": aid, "verdict": "accepted", "summary": "good"}, own)
        self.assertTrue(accepted["recorded"])
        self.assertEqual(self._agent_row(aid)["signoff_verdict"], "accepted")

        # Scheduling a new authorized turn clears prior signoff state.
        daemon.action("send", {"agent_id": aid, "prompt": "round two"}, own)
        row = self._agent_row(aid)
        self.assertIsNone(row["signoff_verdict"])
        self.assertEqual(row["signoff_summary"], "")
        self.assertEqual(row["verification"], "")
        self._wait_terminal(aid)

        daemon.action("update_agent", {"agent_id": aid, "prompt": "round three"}, own)
        self.assertIsNone(self._agent_row(aid)["signoff_verdict"])
        self._wait_terminal(aid)

        self.assertEqual(daemon.action("cancel", {"agent_id": aid}, own)["status"], "cancelled")

    def test_signoff_freshness_gates(self):
        thread = "t-fresh"
        # Non-terminal agent: verdict refused.
        queued_id = self._seed_status(thread, "queued")
        with self.assertRaises(ValueError) as ctx:
            daemon.action("signoff", {"agent_id": queued_id, "verdict": "accepted"}, {"codex_thread_id": thread})
        self.assertIn("not terminal", str(ctx.exception))

        # Failed agent: accepted refused, partial/rejected allowed.
        failed_id = self._seed_status(thread, "failed")
        with self.assertRaises(ValueError) as ctx:
            daemon.action("signoff", {"agent_id": failed_id, "verdict": "accepted"}, {"codex_thread_id": thread})
        self.assertIn("requires a completed agent", str(ctx.exception))
        partial = daemon.action("signoff", {"agent_id": failed_id, "verdict": "partial"}, {"codex_thread_id": thread})
        self.assertTrue(partial["recorded"])
        self.assertEqual(self._agent_row(failed_id)["signoff_verdict"], "partial")

    def test_signoff_stale_gate_read_cannot_record(self):
        """Deterministic gate-vs-write TOCTOU: a stale status read must not smuggle a signoff.

        Simulates the pre-fix race where another writer committed a status
        change right after the old separate gate SELECT: the gate read sees a
        fabricated 'completed' row while the real row is 'queued'. The atomic
        conditional UPDATE evaluates the predicate at write time, so the
        signoff must be refused and never recorded.
        """
        thread = "t-toctou"
        aid = self._seed_status(thread, "queued")
        state = {"connections": 0}
        real_connect = daemon.connect

        class _FakeCursor:
            def __init__(self, row: dict):
                self._row = row

            def fetchone(self):
                return self._row

        def factory():
            state["connections"] += 1
            first_conn = state["connections"] == 1
            gen = real_connect()
            db = gen.__enter__()

            class _Proxy:
                def execute(self, sql, params=()):
                    # Only the FIRST connection's gate SELECT sees the stale
                    # 'completed' row; the post-fix refusal classifier on the
                    # second connection reads the real 'queued' status.
                    if first_conn and sql.lstrip().lower().startswith("select status from agents"):
                        return _FakeCursor({"status": "completed"})
                    return db.execute(sql, params)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    gen.__exit__(*exc)
                    return False

                def __getattr__(self, name):
                    return getattr(db, name)

            return _Proxy()

        with mock.patch.object(daemon, "connect", side_effect=factory):
            with self.assertRaises(ValueError) as ctx:
                daemon.action(
                    "signoff", {"agent_id": aid, "verdict": "accepted"}, {"codex_thread_id": thread}
                )
        self.assertIn("not terminal", str(ctx.exception))
        self.assertIsNone(
            self._agent_row(aid)["signoff_verdict"],
            "a stale gate read must never record a signoff on a queued agent",
        )

    def _seed_status(self, thread_id: str, status: str) -> str:
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (thread_id, thread_id, str(self.root), "test", stamp, stamp),
            )
        aid = str(uuid.uuid4())
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,display_title,hub_token,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, thread_id, "seeded", str(self.root), aid, status, "seeded", "tok-" + uuid.uuid4().hex, stamp, stamp),
            )
        return aid


class MailboxCursorTests(V21Mixin, unittest.TestCase):
    """after_message_id cursor liveness and foreign/expired-cursor isolation."""

    def _send_to_main(self, thread_id: str, from_peer: str, body: str) -> str:
        msg = daemon.MAILBOX.send(
            thread_id=thread_id,
            from_peer=from_peer,
            to_peer=main_peer_id(thread_id),
            body=body,
        )
        return msg.id

    def test_cursor_liveness_and_foreign_isolation(self):
        thread_a, thread_b = "t-cur-a", "t-cur-b"
        aid_a = self._create(thread_a, "cur a")["agent_id"]
        aid_b = self._create(thread_b, "cur b")["agent_id"]
        self._wait_terminal(aid_a)
        self._wait_terminal(aid_b)
        ctx_a = {"codex_thread_id": thread_a}

        m1 = self._send_to_main(thread_a, aid_a, "first")
        m2 = self._send_to_main(thread_a, aid_a, "second")
        m_foreign = self._send_to_main(thread_b, aid_b, "foreign")

        # Live cursor: strictly newer messages only.
        first = daemon.action("wait_any", {"agent_ids": [aid_a], "timeout_seconds": 2}, ctx_a)
        self.assertEqual(first["kind"], "message")
        self.assertEqual(first["message"]["id"], m1)
        self.assertEqual(first["next_cursor"], m1)

        second = daemon.action(
            "wait_any", {"agent_ids": [aid_a], "timeout_seconds": 2, "after_message_id": m1}, ctx_a
        )
        self.assertEqual(second["message"]["id"], m2, "cursor must resume past m1 to strictly newer mail")

        # Foreign cursor: must NOT skip this caller's pending messages.
        foreign = daemon.action(
            "wait_any",
            {"agent_ids": [aid_a], "timeout_seconds": 2, "after_message_id": m_foreign},
            ctx_a,
        )
        self.assertEqual(foreign["message"]["id"], m1, "foreign cursor must not filter the caller inbox")

        # Expired/unknown cursor: same no-skip guarantee.
        expired = daemon.action(
            "wait_any", {"agent_ids": [aid_a], "timeout_seconds": 2, "after_message_id": "no-such-id"}, ctx_a
        )
        self.assertEqual(expired["message"]["id"], m1)

        # The foreign message never leaks into this thread's inbox.
        inbox = daemon.action("hub", {"op": "inbox"}, ctx_a)["messages"]
        self.assertEqual([m["id"] for m in inbox], [m1, m2])
        self.assertNotIn(m_foreign, [m["id"] for m in inbox])

    def test_large_main_body_externalized_worker_lossless(self):
        thread = "t-ext"
        aid = self._create(thread, "ext worker")["agent_id"]
        self._wait_terminal(aid)
        ctx = {"codex_thread_id": thread}
        big = "A" * 9000 + "边界" * 200  # >8192 UTF-8 bytes
        msg_id = self._send_to_main(thread, aid, big)

        # Non-consuming inbox peek so the same mail stays visible to wait_any
        # and the legacy mailbox surface below.
        inbox = daemon.action("hub", {"op": "inbox", "peek": True}, ctx)["messages"]
        self.assertEqual(len(inbox), 1)
        ext = inbox[0]
        self.assertTrue(ext["body_externalized"])
        self.assertEqual(ext["size"], len(big.encode("utf-8")))
        self.assertEqual(ext["sha256"], hashlib.sha256(big.encode("utf-8")).hexdigest())
        self.assertEqual(ext["body"], big[:2000], "Main-facing surface shows only the preview")
        ref = ext["artifact_ref"]
        self.assertEqual(ref["encoding"], "raw-gzip")
        self.assertEqual(ref["size"], ext["size"])
        self.assertEqual(ref["sha256"], ext["sha256"])
        self.assertEqual(ref["relative_path"], ref["path"])
        self.assertTrue(Path(ref["absolute_path"]).exists())
        with gzip.open(Path(ref["absolute_path"]), "rb") as handle:
            self.assertEqual(handle.read(), big.encode("utf-8"), "externalized artifact is lossless")

        # DB row stays byte-for-byte lossless.
        with daemon.connect() as db:
            stored = db.execute("SELECT body FROM agent_messages WHERE id=?", (msg_id,)).fetchone()["body"]
        self.assertEqual(stored, big)

        # Worker/legacy mailbox surface stays byte-for-byte lossless.
        legacy = daemon.MAILBOX.wait(peer_id=main_peer_id(thread), timeout_seconds=1)
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.body, big)

        # wait_any externalizes the same way and returns the cursor.
        first = daemon.action("wait_any", {"agent_ids": [aid], "timeout_seconds": 2}, ctx)
        self.assertEqual(first["kind"], "message")
        self.assertTrue(first["message"]["body_externalized"])
        self.assertEqual(first["message"]["size"], ext["size"])

        # Small bodies are never externalized.
        small_id = self._send_to_main(thread, aid, "short")
        self.assertIsNotNone(small_id)
        small = daemon.action("hub", {"op": "inbox", "peek": True}, ctx)["messages"][-1]
        self.assertFalse(small["body_externalized"])
        self.assertEqual(small["body"], "short")


class BatchTests(V21Mixin, unittest.TestCase):
    """Batch worktree default/item override/legacy + partial failure envelopes."""

    def test_batch_worktree_default_item_override_legacy(self):
        thread = "t-batch-wt"
        repo = self.make_repo()
        ctx = {"codex_thread_id": thread}

        # Batch default True; item 0 explicitly opts out.
        resp = daemon.action(
            "create_agents",
            {
                "agents": [
                    {"agent_name": "shared-a", "prompt": "no isolation", "worktree": False, "cwd": str(repo)},
                    {"agent_name": "iso-b", "prompt": "isolated", "cwd": str(repo)},
                ],
                "worktree": True,
            },
            ctx,
        )
        self.assertEqual(resp["created"], 2)
        shared_a, iso_b = resp["agents"]
        self.assertEqual(self._agent_row(shared_a["agent_id"])["isolation_mode"], "shared")
        self.assertEqual(self._agent_row(iso_b["agent_id"])["isolation_mode"], "worktree")
        self.assertIsNotNone(self._agent_row(iso_b["agent_id"])["worktree_root"])
        self.assertIsNone(self._agent_row(shared_a["agent_id"])["worktree_root"])

        # Batch default False; item explicitly opts in.
        resp2 = daemon.action(
            "create_agents",
            {
                "agents": [
                    {"agent_name": "iso-c", "prompt": "opt in", "worktree": True, "cwd": str(repo)},
                    {"agent_name": "shared-d", "prompt": "shared", "cwd": str(repo)},
                ],
                "worktree": False,
            },
            ctx,
        )
        self.assertEqual(self._agent_row(resp2["agents"][0]["agent_id"])["isolation_mode"], "worktree")
        self.assertEqual(self._agent_row(resp2["agents"][1]["agent_id"])["isolation_mode"], "shared")

        # Legacy: no worktree anywhere -> default profile (shared), no role.
        resp3 = daemon.action(
            "create_agents",
            {"agents": [{"agent_name": "legacy-e", "prompt": "legacy", "cwd": str(repo)}]},
            ctx,
        )
        legacy = self._agent_row(resp3["agents"][0]["agent_id"])
        self.assertEqual(legacy["isolation_mode"], "shared")
        self.assertIsNone(legacy["worktree_root"])
        self.assertIsNone(legacy["role"])

        # Dirty parent untouched: the main repo stays clean throughout.
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"], check=True, capture_output=True
        ).stdout
        self.assertEqual(status, b"")

        for entry in resp["agents"] + resp2["agents"] + resp3["agents"]:
            self._wait_terminal(entry["agent_id"])

    def test_batch_validation_operational_internal_envelope(self):
        thread = "t-batch-cls"
        ctx = {"codex_thread_id": thread}
        real_create = daemon._create_agent_one
        attempted: list[str] = []

        def fake_create(agent_name, prompt, cwd, codex_thread_title, context, **kwargs):
            attempted.append(agent_name)
            if agent_name == "op-item":
                raise daemon.AgentOperationalError("capacity limit reached")
            if agent_name == "boom-item":
                raise RuntimeError("kernel panic")
            return real_create(agent_name, prompt, cwd, codex_thread_title, context, **kwargs)

        with mock.patch.object(daemon, "_create_agent_one", side_effect=fake_create):
            resp = daemon.action(
                "create_agents",
                {
                    "agents": [
                        {"agent_name": "v-item", "prompt": "do v", "cwd": str(self.root)},
                        {"agent_name": "op-item", "prompt": "do op", "cwd": str(self.root)},
                        {"agent_name": "boom-item", "prompt": "do boom", "cwd": str(self.root)},
                        {"agent_name": "never-item", "prompt": "do never", "cwd": str(self.root)},
                    ]
                },
                ctx,
            )

        self.assertEqual(attempted, ["v-item", "op-item", "boom-item"], "internal failure must stop the batch")
        self.assertEqual(resp["created"], 1, "earlier created ids remain exposed")
        self.assertTrue(resp["internal_error"])
        classes = [(e["index"], e["class"]) for e in resp["errors"]]
        self.assertEqual(classes, [(1, "operational"), (2, "internal")])
        self.assertIn("capacity limit reached", resp["errors"][0]["error"])
        self.assertIn("RuntimeError", resp["errors"][1]["error"])

        # The created agent from item 0 is fully visible afterwards.
        created_id = resp["agents"][0]["agent_id"]
        self.assertIn(
            daemon.action("status", {"agent_id": created_id}, ctx)["status"],
            ("queued", "running", "completed"),
            "earlier agent ids remain visible after partial batch failure",
        )
        self._wait_terminal(created_id)
        result = daemon.action("result", {"agent_id": created_id}, ctx)
        self.assertEqual(result["kind"], "agent_result")

    def test_batch_validation_error_and_bad_defaults(self):
        thread = "t-batch-val"
        ctx = {"codex_thread_id": thread}
        resp = daemon.action(
            "create_agents",
            {
                "agents": [
                    {"agent_name": "", "prompt": "missing name"},
                    {"agent_name": "ok-item", "prompt": "fine"},
                ]
            },
            ctx,
        )
        self.assertEqual(resp["created"], 1)
        self.assertEqual([(e["index"], e["class"]) for e in resp["errors"]], [(0, "validation")])

        # Invalid batch-level defaults abort the whole batch before creation.
        before = self._agent_count()
        for bad in (
            {"agents": [{"agent_name": "x", "prompt": "y"}], "worktree": "yes"},
            {"agents": [{"agent_name": "x", "prompt": "y"}], "role": "bogus"},
            {"agents": [{"agent_name": "x", "prompt": "y"}], "reasoning_effort": "bad value!"},
        ):
            with self.assertRaises(ValueError):
                daemon.action("create_agents", bad, ctx)
        self.assertEqual(self._agent_count(), before, "invalid defaults must leave no ghost agents")

    def _agent_count(self) -> int:
        with daemon.connect() as db:
            return int(db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"])


class ArtifactHandoffTests(V21Mixin, unittest.TestCase):
    """Structured artifact refs (absolute+relative/hash/size) incl. cleanup replay."""

    def _seed_worktree_agent(self, thread_id: str) -> tuple[str, Path, dict]:
        repo = self.make_repo()
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (thread_id, thread_id, str(self.root), "test", stamp, stamp),
            )
        aid = str(uuid.uuid4())
        worker_cwd, meta = daemon.create_agent_worktree(repo, aid, repo)
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,display_title,hub_token,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, thread_id, "artifact", str(worker_cwd), aid, "completed", "artifact", "tok-" + uuid.uuid4().hex, stamp, stamp),
            )
            db.execute(
                "UPDATE agents SET worktree_path=?,worktree_root=?,repo_root=?,repo_rel_cwd=?,"
                "worktree_base_sha=?,original_cwd=?,isolation_mode='worktree' WHERE id=?",
                (
                    worker_cwd, meta["worktree_root"], meta["repo_root"], meta["repo_rel_cwd"],
                    meta["worktree_base_sha"], meta["original_cwd"], aid,
                ),
            )
        return aid, repo, meta

    def test_artifact_refs_and_cleanup_replay(self):
        thread = "t-art"
        aid, repo, meta = self._seed_worktree_agent(thread)
        ctx = {"codex_thread_id": thread}
        wt = Path(meta["worktree_root"])
        (wt / "tracked.txt").write_text("modified\n", encoding="utf-8")
        raw_blob = bytes(range(256)) * 64
        (wt / "blob.bin").write_bytes(raw_blob)

        result = daemon.action("result", {"agent_id": aid}, ctx)
        iso = result["isolation"]
        self.assertEqual(iso["mode"], "worktree")
        ref = iso["patch_ref"]
        self.assertIsNotNone(ref)
        self.assertEqual(ref["encoding"], "raw-gzip")
        self.assertEqual(ref["relative_path"], iso["patch_artifact"])
        self.assertEqual(ref["path"], iso["patch_artifact"])
        self.assertEqual(iso["patch_sha256"], ref["sha256"])
        self.assertEqual(iso["patch_size"], ref["size"])
        patch_abs = Path(ref["absolute_path"])
        self.assertTrue(patch_abs.exists())
        with gzip.open(patch_abs, "rb") as handle:
            patch_raw = handle.read()
        self.assertEqual(ref["size"], len(patch_raw))
        self.assertEqual(ref["sha256"], hashlib.sha256(patch_raw).hexdigest())
        expected = subprocess.run(
            ["git", "-C", str(wt), "diff", "--binary", meta["worktree_base_sha"]],
            check=True, capture_output=True,
        ).stdout
        self.assertEqual(patch_raw, expected, "patch artifact is the byte-exact raw diff")

        untracked = iso["untracked_artifacts"]
        self.assertEqual(len(untracked), 1)
        entry = untracked[0]
        self.assertEqual(entry["path"], "blob.bin")
        uref = entry["ref"]
        self.assertEqual(uref["encoding"], "base64")
        self.assertEqual(uref["sha256"], hashlib.sha256(raw_blob).hexdigest())
        self.assertEqual(uref["size"], len(raw_blob))
        self.assertEqual(entry["sha256"], uref["sha256"])
        self.assertEqual(entry["size"], uref["size"])
        self.assertTrue(Path(uref["absolute_path"]).exists())
        with gzip.open(Path(uref["absolute_path"]), "rb") as handle:
            self.assertEqual(base64.b64decode(handle.read()), raw_blob, "untracked artifact is lossless")
        # Hash-addressed storage under the artifacts tree: no whole-file path leak.
        self.assertEqual(Path(uref["relative_path"]).parts[:2], ("data", "artifacts"))
        self.assertIn(uref["sha256"][:16], Path(uref["relative_path"]).name)

        # Cleanup deletes the worktree; evidence replay keeps every ref alive.
        daemon.remove_agent_worktree(str(repo), meta["worktree_root"])
        self.assertFalse(wt.exists())
        result2 = daemon.action("result", {"agent_id": aid}, ctx)
        iso2 = result2["isolation"]
        self.assertEqual(iso2["mode"], "worktree")
        self.assertEqual(iso2["patch_artifact"], ref["relative_path"])
        self.assertEqual(iso2["patch_sha256"], ref["sha256"])
        self.assertEqual(iso2["patch_size"], ref["size"])
        self.assertEqual(iso2["patch_ref"]["sha256"], ref["sha256"])
        self.assertEqual(iso2["patch_ref"]["absolute_path"], str(patch_abs))
        self.assertTrue(Path(iso2["patch_ref"]["absolute_path"]).exists(), "artifact outlives the worktree")
        untracked2 = iso2["untracked_artifacts"]
        self.assertEqual(len(untracked2), 1)
        self.assertEqual(untracked2[0]["artifact"], uref["relative_path"])
        self.assertEqual(untracked2[0]["ref"]["sha256"], uref["sha256"])
        self.assertEqual(untracked2[0]["ref"]["size"], uref["size"])

        # Direct builder replay is reachable after deletion.
        patch_path, replayed = daemon.build_worktree_result(aid)
        self.assertEqual(patch_path, ref["relative_path"])
        self.assertEqual(replayed[0]["sha256"], uref["sha256"])


class ReReviewChainTests(V21Mixin, unittest.TestCase):
    """Artifact A -> Main-authorized fix -> artifact B -> Reviewer references B."""

    def test_artifact_a_b_re_review_chain(self):
        thread = "t-rereview"
        repo = self.make_repo()
        ctx = {"codex_thread_id": thread}

        impl = self._create(thread, "impl", prompt="implement feature", cwd=repo, role="implement", reasoning_effort="high")
        impl_aid = impl["agent_id"]
        self._wait_terminal(impl_aid, timeout=30)
        impl_row = self._agent_row(impl_aid)
        self.assertEqual(impl_row["isolation_mode"], "worktree")
        self.assertEqual(impl_row["reasoning_effort"], "high")
        self.assertEqual(impl_row["role"], "implement")
        wt = Path(impl_row["worktree_root"])
        (wt / "tracked.txt").write_text("version A\n", encoding="utf-8")

        res_a = daemon.action("result", {"agent_id": impl_aid}, ctx)
        ref_a = res_a["isolation"]["patch_ref"]
        self.assertIsNotNone(ref_a, "artifact A must exist")
        path_a, sha_a = ref_a["absolute_path"], ref_a["sha256"]

        # Main accepts result A, then the fix-order delivery must stale the
        # accepted signoff atomically with the queued follow-up turn.
        signed = daemon.action("signoff", {"agent_id": impl_aid, "verdict": "accepted", "summary": "needs fixes"}, ctx)
        self.assertTrue(signed["recorded"])
        self.assertEqual(self._agent_row(impl_aid)["signoff_verdict"], "accepted")

        # Main-authorizes the fix through the real hub send path.
        daemon.action("hub", {"op": "send", "to": impl_aid, "message": "Fix the reported issue and update the result."}, ctx)
        self._wait_terminal(impl_aid, timeout=30)
        row2 = self._agent_row(impl_aid)
        self.assertIsNone(row2["signoff_verdict"], "hub delivery must clear the accepted signoff")
        self.assertEqual(row2["signoff_summary"], "")
        self.assertEqual(row2["verification"], "")
        self.assertEqual(row2["reasoning_effort"], "high", "follow-up must preserve effort")
        self.assertEqual(row2["role"], "implement", "follow-up must reuse the same agent row")
        with daemon.connect() as db:
            turns = db.execute("SELECT turn_no,status FROM turns WHERE agent_id=? ORDER BY turn_no", (impl_aid,)).fetchall()
        self.assertEqual([t["turn_no"] for t in turns], [1, 2], "exactly one Main-authorized follow-up")

        (wt / "tracked.txt").write_text("version B (fixed)\n", encoding="utf-8")
        res_b = daemon.action("result", {"agent_id": impl_aid}, ctx)
        ref_b = res_b["isolation"]["patch_ref"]
        self.assertIsNotNone(ref_b, "artifact B must exist")
        path_b, sha_b = ref_b["absolute_path"], ref_b["sha256"]
        self.assertNotEqual(sha_b, sha_a, "the fix must produce a different artifact")
        self.assertNotEqual(path_b, path_a)
        self.assertTrue(Path(path_b).exists())

        # Fresh reviewer receives the re-review request referencing B, not A.
        rev = self._create(thread, "rev", prompt="review implementation and report findings", cwd=repo, role="review")
        rev_aid = rev["agent_id"]
        self._wait_terminal(rev_aid, timeout=30)
        self.assertEqual(self._agent_row(rev_aid)["isolation_mode"], "shared")
        daemon.action("hub", {"op": "send", "to": rev_aid, "message": f"Re-review using the updated artifact: {path_b}"}, ctx)
        self._wait_terminal(rev_aid, timeout=30)

        with daemon.connect() as db:
            review_turns = db.execute(
                "SELECT turn_no,status,prompt FROM turns WHERE agent_id=? ORDER BY turn_no", (rev_aid,)
            ).fetchall()
        self.assertEqual(len(review_turns), 2, "Main->Reviewer re-review must be allowed")
        self.assertEqual(review_turns[1]["status"], "completed")
        prompt2 = review_turns[1]["prompt"]
        self.assertIn(path_b, prompt2, "re-review request must reference artifact B")
        self.assertNotIn(path_a, prompt2, "re-review request must not reference the stale artifact A")


class MCPContractTests(unittest.TestCase):
    """MCP compact JSON text while structuredContent stays identical + schema fields."""

    def test_result_compact_json_text_matches_structured_content(self):
        data = {
            "kind": "agent_result",
            "agent_id": "x",
            "status": "completed",
            "turns": 2,
            "turn_summary": [{"turn_no": 1, "status": "completed"}],
        }
        captured: list[dict] = []

        def _fake_request(payload: dict, timeout: float = 65) -> dict:
            if payload.get("action") == "ping":
                return {"status": "ok"}
            captured.append(payload)
            return data

        with mock.patch.object(mcp_server, "_state", return_value={"control_port": 1}), (
            mock.patch.object(mcp_server, "_request", side_effect=_fake_request)
        ):
            out = mcp_server.call_tool("result", {"agent_id": "x", "detail": "compact"})

        text = out["content"][0]["text"]
        self.assertEqual(json.loads(text), data, "compact JSON text must parse to the same data")
        self.assertEqual(out["structuredContent"], data, "structuredContent must carry the same data")
        self.assertNotIn("\n", text, "compact serialization is single-line")
        self.assertEqual(text, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(captured[0]["action"], "result")
        self.assertEqual(captured[0]["args"], {"agent_id": "x", "detail": "compact"})

    def test_schema_exposes_v21_fields(self):
        def tool(name: str) -> dict:
            return next(t for t in mcp_server.TOOLS if t["name"] == name)

        result = tool("result")
        detail = result["inputSchema"]["properties"]["detail"]
        self.assertEqual(detail["default"], "compact")
        self.assertEqual(set(detail["enum"]), {"compact", "full"})

        agents = tool("create_agents")
        self.assertEqual(agents["inputSchema"]["properties"]["worktree"]["type"], "boolean")
        items = agents["inputSchema"]["properties"]["agents"]["items"]["properties"]
        self.assertEqual(items["worktree"]["type"], "boolean")

        wait_any = tool("wait_any")
        self.assertEqual(wait_any["inputSchema"]["properties"]["after_message_id"]["type"], "string")


class LinuxProcTests(unittest.TestCase):
    """Linux /proc zombie simulation and unreadable-/proc fallback closure."""

    STAT_Z = b"4242 (worker) Z 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24"
    STAT_R = STAT_Z.replace(b" Z ", b" R ")

    def _posix(self):
        # Route past the Windows OpenProcess branch so the /proc branch under
        # test is actually exercised on any host OS.
        return mock.patch.object(daemon.os, "name", "posix")

    def test_zombie_state_counts_as_not_alive(self):
        with self._posix(), mock.patch("builtins.open", return_value=_StatFile(self.STAT_Z)):
            self.assertFalse(daemon.pid_is_alive(4242), "state Z must count as exited")

    def test_running_state_counts_as_alive(self):
        with self._posix(), mock.patch("builtins.open", return_value=_StatFile(self.STAT_R)):
            self.assertTrue(daemon.pid_is_alive(4242))

    def test_proc_unreadable_falls_back_to_kill_probe(self):
        with self._posix(), mock.patch("builtins.open", side_effect=OSError("no /proc")), (
            mock.patch.object(daemon.os, "kill", return_value=None)
        ):
            self.assertTrue(daemon.pid_is_alive(4242), "kill(0) success means alive")
        with self._posix(), mock.patch("builtins.open", side_effect=OSError("no /proc")), (
            mock.patch.object(daemon.os, "kill", side_effect=OSError("no such process"))
        ):
            self.assertFalse(daemon.pid_is_alive(4242), "kill(0) ESRCH means dead")


class WindowsUtf8DecodeTests(V21Mixin, unittest.TestCase):
    """ACP-936 simulation: grok probes and worktree git helpers decode UTF-8.

    Simulates a non-UTF-8 Windows console code page (the reviewer's ACP 936
    environment) by patching ``locale.getpreferredencoding`` to "cp936" and
    replaying CPython's text-mode subprocess decode path, so the assertions
    fail on any implicit-locale decode (mojibake) and on any strict-decode
    crash (invalid-for-GBK bytes).
    """

    def _decode_run(self, outputs: list[bytes], captured: list[dict]):
        """Replay subprocess text-mode decoding with the kwargs the caller passes."""

        def fake_run(cmd, **kwargs):
            captured.append(kwargs)
            raw = outputs[min(len(outputs) - 1, len(captured) - 1)]
            encoding = kwargs.get("encoding") or locale.getpreferredencoding(False)
            errors = kwargs.get("errors") or "strict"
            text = raw.decode(encoding, errors)
            return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

        return fake_run

    def test_prompt_probe_decodes_utf8_under_cp936(self):
        captured: list[dict] = []
        help_utf8 = "usage: grok -p ...\n  --prompt-file FILE 从文件读取 prompt\n".encode("utf-8")
        fake = self._decode_run([help_utf8], captured)
        prompt_transport._PROBE_CACHE = prompt_transport._UNSET
        try:
            with mock.patch("locale.getpreferredencoding", return_value="cp936"), (
                mock.patch("subprocess.run", side_effect=fake)
            ):
                result = prompt_transport.probe_prompt_file_support()
        finally:
            prompt_transport._PROBE_CACHE = prompt_transport._UNSET
        self.assertEqual(result, "--prompt-file", "UTF-8 help must not mojibake under ACP 936")
        self.assertEqual(captured[0]["encoding"], "utf-8")
        self.assertEqual(captured[0]["errors"], "replace")

    def test_prompt_probe_survives_invalid_gbk_bytes(self):
        # \xff is not a valid GBK lead byte: a strict locale decode would
        # raise UnicodeDecodeError inside the probe (background crash).
        raw = b"\xff\xfe\x00" + "  --prompt-file FILE\n".encode("utf-8")
        fake = self._decode_run([raw], [])
        prompt_transport._PROBE_CACHE = prompt_transport._UNSET
        try:
            with mock.patch("locale.getpreferredencoding", return_value="cp936"), (
                mock.patch("subprocess.run", side_effect=fake)
            ):
                result = prompt_transport.probe_prompt_file_support()
        finally:
            prompt_transport._PROBE_CACHE = prompt_transport._UNSET
        self.assertEqual(result, "--prompt-file", "probe must not crash on non-UTF-8 byte streams")

    def test_native_probe_decodes_utf8_and_survives_invalid_bytes(self):
        captured: list[dict] = []
        version_utf8 = "版本 1.0.0\n".encode("utf-8")
        help_utf8 = "options:\n  --plugin-dir DIR 插件目录\n".encode("utf-8")
        invalid = b"--plugin-dir\xff\xfe\n"
        fake = self._decode_run([version_utf8, help_utf8, invalid], captured)
        with mock.patch("locale.getpreferredencoding", return_value="cp936"), (
            mock.patch("subprocess.run", side_effect=fake)
        ):
            info = native_transport.probe_grok_transport()
        self.assertEqual(info["version"], "版本 1.0.0", "UTF-8 version line must decode cleanly")
        self.assertTrue(info["agent_stdio_plugin_dir_supported"])
        self.assertTrue(info["prompt_mode_plugin_supported"])
        for kwargs in captured:
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")

    def test_worktree_git_helpers_decode_utf8(self):
        """Every text-mode git worktree helper call must carry explicit UTF-8."""
        repo = self.make_repo()
        captured: list[tuple[str, object, object]] = []
        real_run = subprocess.run

        def spy_run(*args, **kwargs):
            if kwargs.get("text"):
                captured.append(("text", kwargs.get("encoding"), kwargs.get("errors")))
            else:
                captured.append(("binary", kwargs.get("encoding"), kwargs.get("errors")))
            return real_run(*args, **kwargs)

        aid = str(uuid.uuid4())
        with mock.patch("subprocess.run", side_effect=spy_run):
            _worker_cwd, meta = daemon.create_agent_worktree(repo, aid, repo)
            daemon.remove_agent_worktree(str(repo), meta["worktree_root"])
        text_calls = [c for c in captured if c[0] == "text"]
        self.assertTrue(text_calls, "the worktree helpers must run text-mode git calls")
        for kind, encoding, errors in text_calls:
            self.assertEqual(encoding, "utf-8", f"{kind} text call must decode as UTF-8")
            self.assertEqual(errors, "replace")
        self.assertFalse(Path(meta["worktree_root"]).exists(), "cleanup still works with the spy")


class MCPTimeoutTests(unittest.TestCase):
    """create_agents MCP timeout scales with item count; other timeouts unchanged."""

    def test_create_agents_timeout_formula(self):
        self.assertEqual(
            mcp_server.request_timeout_for("create_agents", {"agents": [{"agent_name": "a"}]}),
            25,
        )
        self.assertEqual(
            mcp_server.request_timeout_for(
                "create_agents", {"agents": [{"agent_name": "a"}, {"agent_name": "b"}]}
            ),
            35,
        )
        twenty = [{"agent_name": f"a{i}"} for i in range(20)]
        self.assertEqual(mcp_server.request_timeout_for("create_agents", {"agents": twenty}), 215)
        # Non-list shapes must not crash the helper.
        self.assertEqual(mcp_server.request_timeout_for("create_agents", {"agents": "nope"}), 25)
        self.assertEqual(mcp_server.request_timeout_for("create_agents", {}), 25)

    def test_other_action_timeouts_preserved(self):
        self.assertEqual(mcp_server.request_timeout_for("result", {}), 15)
        self.assertEqual(
            mcp_server.request_timeout_for("create_agent", {"agent_name": "x", "prompt": "y"}), 15
        )
        self.assertEqual(mcp_server.request_timeout_for("wait", {"timeout_seconds": 30}), 35)
        self.assertEqual(mcp_server.request_timeout_for("wait_any", {"timeout_seconds": 10}), 15)
        self.assertEqual(mcp_server.request_timeout_for("hub", {"op": "wait", "timeout_seconds": 5}), 10)
        self.assertEqual(mcp_server.request_timeout_for("hub", {"op": "send"}), 15)

    def test_slow_batch_returns_envelope_with_scaled_timeout(self):
        envelope = {"agents": [{"agent_id": "slow-1"}], "created": 1, "errors": []}
        captured: dict = {}

        def _fake_request(payload: dict, timeout: float = 65) -> dict:
            if payload.get("action") == "ping":
                return {"status": "ok"}
            captured["timeout"] = timeout
            time.sleep(1.0)  # controlled slow batch
            return envelope

        with mock.patch.object(mcp_server, "_state", return_value={"control_port": 1}), (
            mock.patch.object(mcp_server, "_request", side_effect=_fake_request)
        ):
            out = mcp_server.call_tool(
                "create_agents", {"agents": [{"agent_name": "a"}, {"agent_name": "b"}]}
            )
        self.assertEqual(captured["timeout"], 35, "a slow batch must be granted the scaled window")
        self.assertEqual(out["structuredContent"], envelope, "the structured envelope must come back")


class MailboxRowidOrderTests(V21Mixin, unittest.TestCase):
    """Same-created_at mail follows insertion (rowid) order, not uuid4 order."""

    def test_same_timestamp_messages_follow_insertion_order(self):
        thread = "t-rowid"
        aid = self._create(thread, "rowid worker")["agent_id"]
        self._wait_terminal(aid)
        inbox = main_peer_id(thread)
        stamp = "2026-08-09T00:00:00.000000+00:00"
        # Inserted z-first but id-sorts AFTER a: (created_at, id) ordering
        # would return 'a-second-inserted' first and break insertion order.
        with daemon.connect() as db:
            for mid in ("z-first-inserted", "a-second-inserted"):
                db.execute(
                    "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,kind,body,delivery_mode,"
                    "state,target_turn_id,error,created_at,delivered_at,consumed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, thread, aid, inbox, "message", "body-" + mid, "queue", "pending", None, None, stamp, None, None),
                )

        first = daemon.MAILBOX.peek_one(peer_id=inbox)
        self.assertIsNotNone(first)
        self.assertEqual(
            first.id, "z-first-inserted", "same-timestamp mail must follow insertion order"
        )
        second = daemon.MAILBOX.peek_one(peer_id=inbox, after_message_id=first.id)
        self.assertEqual(second.id, "a-second-inserted", "the cursor resumes in insertion order")
        self.assertIsNone(
            daemon.MAILBOX.peek_one(peer_id=inbox, after_message_id=second.id),
            "cursor past the last message leaves nothing pending",
        )
        drained = daemon.MAILBOX.inbox(peer_id=inbox)
        self.assertEqual(
            [m.id for m in drained],
            ["z-first-inserted", "a-second-inserted"],
            "the consuming inbox drain follows insertion order too",
        )


class _StatFile:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
