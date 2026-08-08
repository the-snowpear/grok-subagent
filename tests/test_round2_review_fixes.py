"""Regression tests for Agent Fabric Review Fix Round 2.

These tests target bugs found after the first review-fix pass:
- durable queued delivery turns were destroyed by restart recovery;
- auto-injected messages replayed through inbox/wait;
- messages were marked delivered before Grok subprocess start;
- legal large mailbox messages were silently truncated to 4000 chars;
- worktree worker-cwd was confused with the registered worktree root;
- binary untracked worktree files were lossy UTF-8 decoded;
- stale worktree paths could resolve to the main repo root and rmtree it;
- post-commit scheduler errors could turn a durable send into a client error;
- current grok -p workers had no stable fallback-hub discovery path.
"""

from __future__ import annotations

import base64
import gzip
import inspect
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import daemon
from coordination import Mailbox, main_peer_id


class Round2Mixin:
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

    def tearDown(self):
        with daemon.RUNNERS_LOCK:
            for runner in list(daemon.RUNNERS.values()):
                runner.shutdown()
            daemon.RUNNERS.clear()
        for key, value in self.saved.items():
            setattr(daemon, key, value)
        self.tmp.cleanup()

    def seed_task(self, thread_id: str) -> None:
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (thread_id, thread_id, str(self.root), "test", stamp, stamp),
            )

    def seed_agent(self, thread_id: str, status: str = "completed", cwd: str | None = None) -> str:
        self.seed_task(thread_id)
        aid = str(uuid.uuid4())
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,display_title,hub_token,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, thread_id, "worker", cwd or str(self.root), aid, status, "worker", "tok-" + uuid.uuid4().hex, stamp, stamp),
            )
        return aid

    def seed_message(self, thread_id: str, target: str, body: str) -> str:
        mid = str(uuid.uuid4())
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO agent_messages(id,thread_id,from_peer,to_peer,body,created_at) VALUES(?,?,?,?,?,?)",
                (mid, thread_id, main_peer_id(thread_id), target, body, daemon.now()),
            )
        return mid


class DurableDeliveryTests(Round2Mixin, unittest.TestCase):
    def test_recovery_preserves_real_scheduled_queued_delivery(self):
        thread_id = "round2-recover"
        aid = self.seed_agent(thread_id, "completed")
        mid = self.seed_message(thread_id, aid, "hello after completion")
        with mock.patch.object(daemon, "get_runner", return_value=None):
            self.assertEqual(daemon.maybe_schedule_delivery(aid), 1)
        with daemon.connect() as db:
            before_agent = db.execute("SELECT status FROM agents WHERE id=?", (aid,)).fetchone()
            before_turn = db.execute("SELECT id,status FROM turns WHERE agent_id=?", (aid,)).fetchone()
            before_msg = db.execute("SELECT target_turn_id,state FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(before_agent["status"], "queued")
        self.assertEqual(before_turn["status"], "queued")
        self.assertEqual(before_msg["target_turn_id"], before_turn["id"])
        self.assertEqual(before_msg["state"], "pending")

        daemon.recover(start_runners=False)
        with daemon.connect() as db:
            agent = db.execute("SELECT status FROM agents WHERE id=?", (aid,)).fetchone()
            turn = db.execute("SELECT status FROM turns WHERE id=?", (before_turn["id"],)).fetchone()
            msg = db.execute("SELECT target_turn_id,state FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(agent["status"], "queued")
        self.assertEqual(turn["status"], "queued")
        self.assertEqual(msg["target_turn_id"], before_turn["id"])
        self.assertEqual(msg["state"], "pending")

    def test_scheduled_message_hidden_then_consumed_after_delivery(self):
        thread_id = "round2-consume"
        aid = self.seed_agent(thread_id, "completed")
        mid = self.seed_message(thread_id, aid, "one-shot")
        with mock.patch.object(daemon, "get_runner", return_value=None):
            daemon.maybe_schedule_delivery(aid)
        with daemon.connect() as db:
            turn_id = int(db.execute("SELECT id FROM turns WHERE agent_id=?", (aid,)).fetchone()["id"])
        self.assertEqual(daemon.MAILBOX.inbox(peer_id=aid, peek=True), [])
        self.assertEqual(daemon.MAILBOX.mark_delivered_for_turn(turn_id=turn_id), 1)
        self.assertEqual(daemon.MAILBOX.inbox(peer_id=aid, peek=True), [])
        with daemon.connect() as db:
            row = db.execute("SELECT state,delivered_at,consumed_at FROM agent_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(row["state"], "delivered")
        self.assertTrue(row["delivered_at"])
        self.assertTrue(row["consumed_at"])

    def test_large_single_message_spills_full_body_to_artifact_without_truncation(self):
        thread_id = "round2-large"
        aid = self.seed_agent(thread_id, "completed")
        body = "大" * 21000  # ~63 KiB UTF-8: legal mailbox message, >60 KiB envelope.
        self.seed_message(thread_id, aid, body)
        with mock.patch.object(daemon, "get_runner", return_value=None):
            self.assertEqual(daemon.maybe_schedule_delivery(aid), 1)
        with daemon.connect() as db:
            prompt = str(db.execute("SELECT prompt FROM turns WHERE agent_id=?", (aid,)).fetchone()["prompt"])
        self.assertNotIn("…[截断]", prompt)
        self.assertIn("完整 UTF-8 内容已保存", prompt)
        artifacts = list((daemon.ARTIFACTS / aid).glob("hub-message-*.txt.gz"))
        self.assertEqual(len(artifacts), 1)
        with gzip.open(artifacts[0], "rt", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), body)

    def test_post_commit_scheduler_failure_does_not_fail_send(self):
        thread_id = "round2-fail-open"
        self.seed_task(thread_id)

        def explode(_message):
            raise RuntimeError("scheduler unavailable")

        mailbox = Mailbox(daemon.coordination_connect, daemon.now, on_message_committed=explode)
        message = mailbox.send(
            thread_id=thread_id,
            from_peer=main_peer_id(thread_id),
            to_peer="virtual-target",
            body="durable first",
        )
        with daemon.connect() as db:
            row = db.execute("SELECT body,state FROM agent_messages WHERE id=?", (message.id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["body"], "durable first")
        self.assertEqual(row["state"], "pending")

    def test_runner_source_marks_delivery_only_after_popen_and_has_release_path(self):
        source = inspect.getsource(daemon.AgentRunner._run)
        self.assertLess(source.index("subprocess.Popen"), source.index("mark_delivered_for_turn"))
        self.assertIn("release_scheduled_for_turn", source)


class WorktreeRound2Tests(Round2Mixin, unittest.TestCase):
    def make_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "round2@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Round2"], cwd=repo, check=True)
        (repo / "subdir").mkdir()
        (repo / "subdir" / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        return repo

    def test_subdir_worker_cwd_is_separate_from_registered_worktree_root_and_cleanup(self):
        repo = self.make_repo()
        aid = str(uuid.uuid4())
        worker_cwd, meta = daemon.create_agent_worktree(repo / "subdir", aid, repo / "subdir")
        self.assertEqual(Path(worker_cwd).name, "subdir")
        self.assertEqual(Path(meta["worktree_root"]).parent.name, "worktrees")
        self.assertNotEqual(Path(worker_cwd).resolve(), Path(meta["worktree_root"]).resolve())
        roots = subprocess.check_output(["git", "-C", str(repo), "worktree", "list", "--porcelain"], text=True)
        self.assertIn(meta["worktree_root"].replace("\\", "/"), roots)
        daemon.remove_agent_worktree(str(repo), meta["worktree_root"])
        roots_after = subprocess.check_output(["git", "-C", str(repo), "worktree", "list", "--porcelain"], text=True)
        self.assertNotIn(meta["worktree_root"].replace("\\", "/"), roots_after)
        self.assertFalse(Path(meta["worktree_root"]).exists())

    def test_binary_untracked_file_is_lossless_base64_artifact(self):
        repo = self.make_repo()
        thread_id = "round2-binary"
        aid = self.seed_agent(thread_id, "completed")
        worker_cwd, meta = daemon.create_agent_worktree(repo, aid, repo)
        binary = bytes(range(256)) + b"\x00\xff\xfe\x80"
        (Path(meta["worktree_root"]) / "new.bin").write_bytes(binary)
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET cwd=?,worktree_path=?,worktree_root=?,repo_root=?,repo_rel_cwd=?,"
                "worktree_base_sha=?,original_cwd=?,isolation_mode='worktree' WHERE id=?",
                (
                    worker_cwd, worker_cwd, meta["worktree_root"], str(repo), ".",
                    meta["worktree_base_sha"], str(repo), aid,
                ),
            )
        _patch, untracked = daemon.build_worktree_result(aid)
        entry = next(item for item in untracked if item["path"] == "new.bin")
        self.assertEqual(entry["encoding"], "base64")
        artifact_path = daemon.ROOT / entry["artifact"]
        with gzip.open(artifact_path, "rt", encoding="utf-8") as handle:
            restored = base64.b64decode(handle.read())
        self.assertEqual(restored, binary)
        daemon.remove_agent_worktree(str(repo), meta["worktree_root"])

    def test_stale_worktree_never_resolves_to_main_root_and_remove_is_safe(self):
        repo = self.make_repo()
        aid = str(uuid.uuid4())
        _worker_cwd, meta = daemon.create_agent_worktree(repo, aid, repo)
        stale_root = meta["worktree_root"]
        shutil.rmtree(stale_root, ignore_errors=True)

        def assert_not_main_root(candidate: str) -> None:
            resolved = daemon._resolve_worktree_root(str(repo), candidate)
            if resolved is not None:
                self.assertNotEqual(
                    os.path.normcase(os.path.realpath(resolved)),
                    os.path.normcase(os.path.realpath(str(repo))),
                )

        assert_not_main_root(stale_root)
        daemon.remove_agent_worktree(str(repo), stale_root)
        self.assertTrue(repo.exists())
        self.assertTrue((repo / ".git").exists())

        stale_subdir = stale_root + "subdir"
        assert_not_main_root(stale_subdir)
        daemon.remove_agent_worktree(str(repo), stale_subdir)
        self.assertTrue(repo.exists())
        self.assertTrue((repo / ".git").exists())

        thread_id = "round2-stale"
        stale_aid = self.seed_agent(thread_id, "completed")
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET worktree_path=?,worktree_root=?,repo_root=?,worktree_base_sha=? WHERE id=?",
                (stale_root, stale_root, str(repo), meta["worktree_base_sha"], stale_aid),
            )
        self.assertEqual(daemon.build_worktree_result(stale_aid), (None, []))


class DiscoveryAndStartupTests(Round2Mixin, unittest.TestCase):
    def test_worker_bridge_env_exposes_absolute_cli_and_native_bridge_paths(self):
        thread_id = "round2-env"
        aid = self.seed_agent(thread_id, "completed")
        real_root = self.saved["ROOT"]
        with mock.patch.object(daemon, "ROOT", real_root):
            env = daemon.worker_bridge_env(aid)
        self.assertTrue(Path(env["GROK_OBSERVER_HUB_CLI"]).is_absolute())
        self.assertTrue(Path(env["GROK_OBSERVER_NATIVE_BRIDGE"]).is_absolute())
        self.assertTrue(env["GROK_OBSERVER_HUB_CLI"].endswith("grok_hub.py"))
        self.assertTrue(env["GROK_OBSERVER_NATIVE_BRIDGE"].endswith("native_bridge.py"))
        self.assertEqual(env["GROK_OBSERVER_AGENT_ID"], aid)

    def test_main_binds_ports_before_starting_recovered_runners(self):
        source = inspect.getsource(daemon.main)
        self.assertLess(source.index("worker_server.serve_forever"), source.index("recover_runners()"))


if __name__ == "__main__":
    unittest.main()
