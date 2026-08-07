"""Tests for PR D: agent profiles, typed result payloads, and worktree isolation.

Covers profile resolution (default/fast/deep/isolated), explicit worktree and
max_turns overrides, git worktree creation/cleanup, the extracted
daemon.grok_command shape, the typed agent_result payload with changes, and
max_turns validation.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import daemon


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
        # Never attempt to launch a browser from these tests.
        self._prev_no_browser = os.environ.get("GROK_OBSERVER_NO_BROWSER")
        os.environ["GROK_OBSERVER_NO_BROWSER"] = "1"
        # Room for the batch tests; deterministic fake Grok for real creates.
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

    def _make_git_repo(self, folder: Path) -> Path:
        """Initialize a git repository with one committed file."""
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=folder, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"],
            cwd=folder, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "t"],
            cwd=folder, check=True, capture_output=True,
        )
        # Deterministic LF checkouts so worktree diff/apply round-trips exactly.
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=folder, check=True, capture_output=True,
        )
        (folder / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=folder, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=folder, check=True, capture_output=True)
        return folder

    @staticmethod
    def _normpath(value: str) -> str:
        """Normalize path separators so git output compares with pathlib strings."""
        return str(value).replace("\\", "/")

    def _repo_head(self, repo: Path) -> str:
        """Return the current HEAD sha of a repository."""
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def _seed_agent(
        self,
        thread_id: str = "t",
        name: str = "a",
        status: str = "completed",
        max_turns=None,
        worktree_path=None,
    ) -> str:
        """Insert a worker row directly; returns its agent id. Never spawns Grok."""
        agent_id = str(uuid.uuid4())
        stamp = daemon.now()
        columns = ["id", "thread_id", "name", "cwd", "grok_session_id", "status", "created_at", "updated_at"]
        values = [agent_id, thread_id, name, ".", agent_id, status, stamp, stamp]
        if max_turns is not None:
            columns.append("max_turns")
            values.append(int(max_turns))
        if worktree_path is not None:
            columns.append("worktree_path")
            values.append(str(worktree_path))
        with daemon.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (thread_id, thread_id, ".", "test", stamp, stamp),
            )
            db.execute(
                f"INSERT INTO agents({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                values,
            )
        return agent_id

    def _agent_row(self, agent_id: str) -> dict:
        with daemon.connect() as db:
            return dict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())

    def _turn_prompt(self, agent_id: str) -> str:
        with daemon.connect() as db:
            row = db.execute(
                "SELECT prompt FROM turns WHERE agent_id=? AND turn_no=1", (agent_id,)
            ).fetchone()
            return row["prompt"] if row else ""

    def _create(self, prompt: str = "do work", name: str = "t", cwd: str | None = None, **extra) -> dict:
        args = {"agent_name": name, "prompt": prompt}
        if cwd is not None:
            args["cwd"] = cwd
        args.update(extra)
        return daemon.action("create_agent", args, {"codex_thread_id": "t"})

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


class ProfilesWorktreeTest(_IsolatedDbMixin, unittest.TestCase):
    def test_default_profile_no_worktree(self):
        repo = self._make_git_repo(self.folder / "repo")
        created = self._create("p", cwd=str(repo))
        row = self._agent_row(created["agent_id"])
        self.assertIsNone(row["worktree_path"])
        self.assertEqual(row["max_turns"], 50)
        self.assertEqual(row["cwd"], str(repo.resolve()))

    def test_deep_profile_creates_worktree(self):
        repo = self._make_git_repo(self.folder / "repo")
        created = self._create("p", cwd=str(repo), profile="deep")
        row = self._agent_row(created["agent_id"])
        self.assertIsNotNone(row["worktree_path"])
        self.assertTrue(os.path.isdir(row["worktree_path"]))
        self.assertEqual(row["cwd"], row["worktree_path"])
        self.assertEqual(row["max_turns"], 100)
        self.assertIn("深度模式", self._turn_prompt(created["agent_id"]))

    def test_worktree_requires_git_repo(self):
        plain = self.folder / "plain"
        plain.mkdir(parents=True, exist_ok=True)
        with self.assertRaisesRegex(ValueError, "worktree isolation requires a git repository"):
            self._create("p", cwd=str(plain), profile="deep")
        with daemon.connect() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_unknown_profile_raises(self):
        repo = self._make_git_repo(self.folder / "repo")
        with self.assertRaisesRegex(ValueError, "unknown profile"):
            self._create("p", cwd=str(repo), profile="bogus")

    def test_explicit_overrides_profile(self):
        repo = self._make_git_repo(self.folder / "repo")
        created = self._create("p", cwd=str(repo), profile="deep", worktree=False, max_turns=30)
        row = self._agent_row(created["agent_id"])
        self.assertEqual(row["max_turns"], 30)
        self.assertIsNone(row["worktree_path"])
        self.assertEqual(row["cwd"], str(repo.resolve()))
        self.assertFalse((daemon.DATA / "worktrees" / created["agent_id"]).exists())

    def test_grok_command_shape(self):
        row = {"max_turns": 100, "grok_session_id": "s"}
        cmd = daemon.grok_command(row, "p", True, Path("."))
        self.assertEqual(cmd[0], sys.executable)
        self.assertIn(str(FAKE_GROK), cmd)
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "100")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "s")
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--cwd") + 1], str(Path(".")))

        resumed = daemon.grok_command(row, "p", False, Path("."))
        self.assertEqual(resumed[resumed.index("--resume") + 1], "s")
        self.assertNotIn("--session-id", resumed)

        defaulted = daemon.grok_command({"grok_session_id": "s2"}, "p", True, Path("."))
        self.assertEqual(defaulted[defaulted.index("--max-turns") + 1], "50")

    def test_result_typed_fields(self):
        agent_id = self._seed_agent()
        stamp = daemon.now()
        with daemon.connect() as db:
            for path, kind, added, deleted in (
                ("a.txt", "modified", 2, 1),
                ("b.txt", "added", 3, 0),
            ):
                db.execute(
                    "INSERT INTO changes(agent_id,turn_id,path,kind,added,deleted,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (agent_id, 1, path, kind, added, deleted, stamp),
                )
        result = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual(result["kind"], "agent_result")
        self.assertEqual(len(result["changes"]), 2)
        self.assertEqual([c["path"] for c in result["changes"]], ["a.txt", "b.txt"])
        self.assertEqual(result["changes"][0]["added"], 2)
        self.assertEqual(result["changes"][1]["deleted"], 0)

    def test_cleanup_removes_worktree(self):
        repo = self._make_git_repo(self.folder / "repo")
        created = self._create("p", cwd=str(repo), profile="deep")
        agent_id = created["agent_id"]
        self._wait_terminal(agent_id)
        worktree_path = self._agent_row(agent_id)["worktree_path"]
        self.assertTrue(os.path.isdir(worktree_path))
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with daemon.connect() as db:
            db.execute("UPDATE agents SET updated_at=? WHERE id=?", (old, agent_id))
        with mock.patch.object(daemon, "RETENTION_DAYS", 0):
            daemon.cleanup()
        self.assertFalse(os.path.isdir(worktree_path))
        with daemon.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone())

    def test_create_agents_passes_profile(self):
        result = daemon.action(
            "create_agents",
            {
                "agents": [
                    {"agent_name": "a1", "prompt": "p1", "profile": "fast"},
                    {"agent_name": "a2", "prompt": "p2", "profile": "fast"},
                ]
            },
            {"codex_thread_id": "t"},
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["errors"], [])
        with daemon.connect() as db:
            rows = db.execute("SELECT name,max_turns FROM agents WHERE thread_id='t' ORDER BY name").fetchall()
        self.assertEqual([(r["name"], r["max_turns"]) for r in rows], [("a1", 20), ("a2", 20)])
        for name in ("a1", "a2"):
            with daemon.connect() as db:
                agent_id = db.execute("SELECT id FROM agents WHERE name=?", (name,)).fetchone()["id"]
            self.assertIn("快速模式", self._turn_prompt(agent_id))

    def test_max_turns_validation(self):
        plain = self.folder / "plain"
        plain.mkdir(parents=True, exist_ok=True)
        with self.assertRaisesRegex(ValueError, "max_turns must be between 1 and 500"):
            self._create("p", cwd=str(plain), max_turns=0)
        with self.assertRaisesRegex(ValueError, "max_turns must be between 1 and 500"):
            self._create("p", cwd=str(plain), max_turns=501)
        with self.assertRaisesRegex(ValueError, "max_turns must be an integer"):
            self._create("p", cwd=str(plain), max_turns="abc")
        with daemon.connect() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_isolated_profile_worktree(self):
        repo = self._make_git_repo(self.folder / "repo")
        created = self._create("p", cwd=str(repo), profile="isolated")
        row = self._agent_row(created["agent_id"])
        self.assertIsNotNone(row["worktree_path"])
        self.assertTrue(os.path.isdir(row["worktree_path"]))
        self.assertEqual(row["cwd"], row["worktree_path"])
        self.assertEqual(row["max_turns"], 50)

    def test_worktree_metadata_persisted(self):
        repo = self._make_git_repo(self.folder / "repo")
        head = self._repo_head(repo)
        created = self._create("p", cwd=str(repo), profile="deep")
        row = self._agent_row(created["agent_id"])
        self.assertEqual(self._normpath(row["original_cwd"]), self._normpath(repo.resolve()))
        self.assertEqual(self._normpath(row["repo_root"]), self._normpath(repo.resolve()))
        self.assertEqual(row["repo_rel_cwd"], ".")
        self.assertEqual(row["worktree_base_sha"], head)
        self.assertEqual(row["isolation_mode"], "worktree")
        expected = daemon.DATA / "worktrees" / created["agent_id"]
        self.assertEqual(self._normpath(row["worktree_path"]), self._normpath(expected))
        self.assertTrue(os.path.isdir(row["worktree_path"]))

    def test_subdir_worker_cwd(self):
        repo = self._make_git_repo(self.folder / "repo")
        subdir = repo / "subdir"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "inner.txt").write_text("inner\n", encoding="utf-8")
        subprocess.run(["git", "add", "subdir/inner.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "subdir"], cwd=repo, check=True, capture_output=True)
        created = self._create("p", cwd=str(subdir), profile="deep")
        row = self._agent_row(created["agent_id"])
        self.assertEqual(row["repo_rel_cwd"], "subdir")
        self.assertTrue(self._normpath(row["worktree_path"]).endswith("/subdir"))
        self.assertTrue(os.path.isdir(row["worktree_path"]))
        self.assertEqual(row["cwd"], row["worktree_path"])

    def test_create_failure_cleans_up_worktree(self):
        repo = self._make_git_repo(self.folder / "repo")
        with mock.patch.object(daemon, "MAX_QUEUE_DEPTH", 0):
            with self.assertRaisesRegex(ValueError, "队列已满"):
                self._create("p", cwd=str(repo), profile="deep")
        with daemon.connect() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
        self.assertEqual(count, 0)
        self.assertEqual(list((daemon.DATA / "worktrees").glob("*")), [])

    def test_manual_delete_removes_worktree(self):
        repo = self._make_git_repo(self.folder / "repo")
        created = self._create("p", cwd=str(repo), profile="isolated")
        agent_id = created["agent_id"]
        self._wait_terminal(agent_id)
        worktree_path = self._agent_row(agent_id)["worktree_path"]
        self.assertTrue(os.path.isdir(worktree_path))
        server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ViewerHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/agents/{agent_id}/delete",
                method="POST",
                data=b"",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode())
            self.assertTrue(payload["deleted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(os.path.isdir(worktree_path))
        with daemon.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone())
        listed = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertNotIn(self._normpath(worktree_path), self._normpath(listed))

    def test_result_isolation_contract_and_patch(self):
        repo = self._make_git_repo(self.folder / "repo")
        head = self._repo_head(repo)
        created = self._create("p", cwd=str(repo), profile="isolated")
        agent_id = created["agent_id"]
        self._wait_terminal(agent_id)
        worktree_path = Path(self._agent_row(agent_id)["worktree_path"])
        (worktree_path / "tracked.txt").write_text("base\nappended\n", encoding="utf-8", newline="\n")
        (worktree_path / "new.txt").write_text("fresh content\n", encoding="utf-8", newline="\n")
        stamp = daemon.now()
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO changes(agent_id,turn_id,path,kind,added,deleted,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (agent_id, 1, "tracked.txt", "modified", 2, 1, stamp),
            )
        result = daemon.action("result", {"agent_id": agent_id}, {})
        isolation = result["isolation"]
        self.assertEqual(isolation["mode"], "worktree")
        self.assertEqual(isolation["base_sha"], head)
        self.assertEqual(self._normpath(isolation["repo_root"]), self._normpath(repo.resolve()))
        self.assertEqual(self._normpath(isolation["original_cwd"]), self._normpath(repo.resolve()))
        self.assertEqual(self._normpath(isolation["worktree_path"]), self._normpath(worktree_path))
        self.assertIsNotNone(isolation["patch_artifact"])
        self.assertEqual(
            isolation["untracked_artifacts"],
            [{"path": "new.txt", "artifact": isolation["untracked_artifacts"][0]["artifact"]}],
        )
        self.assertIn("tracked.txt", isolation["changed_files"])
        # The patch artifact must replay cleanly onto a fresh clone at base_sha.
        patch_rel = isolation["patch_artifact"]
        with gzip.open(daemon.ROOT / patch_rel, "rt", encoding="utf-8") as handle:
            patch_text = handle.read()
        self.assertIn("appended", patch_text)
        untracked_rel = isolation["untracked_artifacts"][0]["artifact"]
        with gzip.open(daemon.ROOT / untracked_rel, "rt", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "fresh content\n")
        clone_dir = self.folder / "clone"
        subprocess.run(["git", "clone", str(repo), str(clone_dir)], check=True, capture_output=True)
        subprocess.run(["git", "checkout", head], cwd=clone_dir, check=True, capture_output=True)
        patch_file = self.folder / "worktree.patch"
        patch_file.write_text(patch_text, encoding="utf-8")
        subprocess.run(["git", "apply", str(patch_file)], cwd=clone_dir, check=True, capture_output=True)
        self.assertEqual(
            (clone_dir / "tracked.txt").read_text(encoding="utf-8"),
            "base\nappended\n",
        )

    def test_dirty_parent_warning(self):
        repo = self._make_git_repo(self.folder / "repo")
        (repo / "tracked.txt").write_text("base\ndirty\n", encoding="utf-8")
        created = self._create("p", cwd=str(repo), profile="deep")
        with daemon.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE agent_id=? AND type='worktree_warning'",
                (created["agent_id"],),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload"] or "{}")
        self.assertTrue(payload.get("dirty_parent"))
        self.assertEqual(payload.get("base_sha"), self._repo_head(repo))

    def test_shared_isolation_contract(self):
        agent_id = self._seed_agent()
        result = daemon.action("result", {"agent_id": agent_id}, {})
        self.assertEqual(result["isolation"], {"mode": "shared"})


if __name__ == "__main__":
    unittest.main()
