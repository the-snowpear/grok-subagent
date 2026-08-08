"""Regression tests for Agent Fabric Review Fix Round 3, commit 2.

Covers the worktree cleanup fail-closed guarantee (P1-D) and the lossless
tracked-patch artifact (P2-A):
- removal is structurally confined to DATA/worktrees descendants even when
  repo_root metadata is NULL or stale, so the main repository can never be
  resolved as a deletion target;
- legacy worker-subdir rows still resolve to their registered worktree root;
- build_worktree_result refuses to snapshot the main repo even from poisoned
  metadata;
- tracked patches are stored as raw-gzip bytes (byte-preserving for non-UTF-8
  diff payloads) with size/hash metadata, verified by clone + git apply.
"""

from __future__ import annotations

import gzip
import hashlib
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import daemon


class Round3Mixin:
    """Self-contained fixture: temp daemon paths, fresh DB, and git helpers."""

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

    def seed_agent(self, thread_id: str, status: str = "completed") -> str:
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
                (aid, thread_id, "worker", str(self.root), aid, status, "worker", "tok-" + uuid.uuid4().hex, stamp, stamp),
            )
        return aid

    def make_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "round3@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Round3"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
        (repo / "subdir").mkdir()
        (repo / "subdir" / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        return repo

    def register_worktree_row(self, aid: str, worker_cwd: str, meta: dict) -> None:
        """Point an already-seeded agent row at a real worktree (mirrors the create path)."""
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET cwd=?,worktree_path=?,worktree_root=?,repo_root=?,repo_rel_cwd=?,"
                "worktree_base_sha=?,original_cwd=?,isolation_mode='worktree' WHERE id=?",
                (
                    worker_cwd, worker_cwd, meta["worktree_root"], meta["repo_root"], meta["repo_rel_cwd"],
                    meta["worktree_base_sha"], meta["original_cwd"], aid,
                ),
            )

    def assert_repo_intact(self, repo: Path) -> None:
        self.assertTrue(repo.exists())
        self.assertTrue((repo / ".git").exists())


class WorktreeRound3Tests(Round3Mixin, unittest.TestCase):
    def test_remove_refuses_main_repo_when_repo_root_none(self):
        repo = self.make_repo()
        marker = repo / "marker.txt"
        marker.write_text("keep me\n", encoding="utf-8")
        # repo_root=NULL: the git-based resolution would return the main repo
        # root; only the structural containment guard can refuse it.
        daemon.remove_agent_worktree(None, str(repo))
        self.assertTrue(marker.exists())
        self.assert_repo_intact(repo)

    def test_remove_refuses_main_repo_when_repo_root_stale(self):
        repo = self.make_repo()
        marker = repo / "marker.txt"
        marker.write_text("keep me\n", encoding="utf-8")
        daemon.remove_agent_worktree(str(repo) + "-stale", str(repo))
        self.assertTrue(marker.exists())
        self.assert_repo_intact(repo)

    def test_remove_refuses_candidate_outside_data_worktrees(self):
        repo = self.make_repo()
        subdir = repo / "subdir"
        tracked = subdir / "tracked.txt"
        daemon.remove_agent_worktree(str(repo), str(subdir))
        self.assertTrue(subdir.exists())
        self.assertTrue(tracked.exists())
        self.assert_repo_intact(repo)

    def test_remove_accepts_registered_root_under_data_worktrees(self):
        repo = self.make_repo()
        aid = str(uuid.uuid4())
        _worker_cwd, meta = daemon.create_agent_worktree(repo, aid, repo)
        roots = subprocess.check_output(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"], text=True
        )
        self.assertIn(meta["worktree_root"].replace("\\", "/"), roots)
        daemon.remove_agent_worktree(str(repo), meta["worktree_root"])
        self.assertFalse(Path(meta["worktree_root"]).exists())
        self.assert_repo_intact(repo)
        roots_after = subprocess.check_output(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"], text=True
        )
        self.assertNotIn(meta["worktree_root"].replace("\\", "/"), roots_after)

    def test_legacy_subdir_resolves_to_safe_registered_root(self):
        repo = self.make_repo()
        aid = str(uuid.uuid4())
        worker_cwd, meta = daemon.create_agent_worktree(repo / "subdir", aid, repo / "subdir")
        self.assertEqual(Path(worker_cwd).name, "subdir")
        legacy = str(Path(meta["worktree_root"]) / "subdir")
        daemon.remove_agent_worktree(str(repo), legacy)
        self.assertFalse(Path(meta["worktree_root"]).exists())
        self.assert_repo_intact(repo)

    def test_build_result_refuses_main_repo_fallback(self):
        repo = self.make_repo()
        marker = repo / "marker.txt"
        marker.write_text("uncommitted\n", encoding="utf-8")
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        thread_id = "round3-main-fallback"
        aid = self.seed_agent(thread_id, "completed")
        # Poisoned/stale metadata: worktree_root points at the MAIN repo and
        # repo_root is NULL, so git-based resolution yields the main root.
        with daemon.connect() as db:
            db.execute(
                "UPDATE agents SET worktree_path=?,worktree_root=?,repo_root=?,worktree_base_sha=? WHERE id=?",
                (str(repo), str(repo), None, head, aid),
            )
        self.assertEqual(daemon.build_worktree_result(aid), (None, []))
        # The repo's own uncommitted changes are not snapshotted.
        self.assertTrue(marker.exists())
        self.assert_repo_intact(repo)

    def test_tracked_non_utf8_patch_roundtrips_bytes(self):
        repo = self.root / "binary-repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "round3@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Round3"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
        original = bytes(range(256))
        (repo / "blob.bin").write_bytes(original)
        subprocess.run(["git", "add", "blob.bin"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "binary base"], cwd=repo, check=True, capture_output=True)

        thread_id = "round3-binary"
        aid = self.seed_agent(thread_id, "completed")
        worker_cwd, meta = daemon.create_agent_worktree(repo, aid, repo)
        modified = bytes(range(255, -1, -1))  # all high-bit values, not valid UTF-8
        self.assertNotEqual(modified, original)
        (Path(meta["worktree_root"]) / "blob.bin").write_bytes(modified)
        self.register_worktree_row(aid, worker_cwd, meta)

        patch_path, _untracked = daemon.build_worktree_result(aid)
        self.assertIsNotNone(patch_path)
        # The stored artifact must byte-match the exact git diff --binary output.
        expected = subprocess.run(
            ["git", "-C", meta["worktree_root"], "diff", "--binary", meta["worktree_base_sha"]],
            check=True, capture_output=True,
        ).stdout
        self.assertTrue(expected)
        with gzip.open(daemon.ROOT / patch_path, "rb") as handle:
            raw_patch = handle.read()
        self.assertEqual(raw_patch, expected)

        # Metadata accompanies the patch in the result isolation contract.
        result = daemon.action("result", {"agent_id": aid}, {})
        isolation = result["isolation"]
        self.assertEqual(isolation["patch_artifact"], patch_path)
        self.assertEqual(isolation["patch_encoding"], "raw-gzip")
        self.assertEqual(isolation["patch_size"], len(expected))
        self.assertEqual(isolation["patch_sha256"], hashlib.sha256(expected).hexdigest())

        # The stored patch must replay onto a fresh clone at base_sha.
        clone = self.root / "clone"
        subprocess.run(["git", "clone", str(repo), str(clone)], check=True, capture_output=True)
        patch_file = self.root / "patch.bin"
        patch_file.write_bytes(raw_patch)
        subprocess.run(["git", "-C", str(clone), "apply", str(patch_file)], check=True, capture_output=True)
        self.assertEqual((clone / "blob.bin").read_bytes(), modified)

        daemon.remove_agent_worktree(str(repo), meta["worktree_root"])

    def test_remove_refuses_when_base_missing(self):
        repo = self.make_repo()
        marker = repo / "marker.txt"
        marker.write_text("keep me\n", encoding="utf-8")
        missing_data = self.root / "no-worktrees"
        self.assertFalse((missing_data / "worktrees").exists())
        with mock.patch.object(daemon, "DATA", missing_data):
            # Structural guard cannot establish containment without the base.
            self.assertIsNone(daemon._safe_worktree_delete_target(str(repo)))
            daemon.remove_agent_worktree(None, str(repo))
            daemon.remove_agent_worktree(str(repo), str(repo))
        self.assertTrue(marker.exists())
        self.assert_repo_intact(repo)


if __name__ == "__main__":
    unittest.main()
