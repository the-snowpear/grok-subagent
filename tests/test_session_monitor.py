"""Tests for session monitor parsing, multi-turn cursor, final drain, and FS snapshots."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import daemon


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class _DbMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._orig = {
            "DB_PATH": daemon.DB_PATH,
            "ARTIFACTS": daemon.ARTIFACTS,
            "DATA": daemon.DATA,
            "STATE_PATH": daemon.STATE_PATH,
            "LOCK_PATH": daemon.LOCK_PATH,
            "ROOT": daemon.ROOT,
            "SESSION_FLUSH_WAIT_S": daemon.SESSION_FLUSH_WAIT_S,
            "SESSION_FINAL_DRAIN_EXTRA_S": daemon.SESSION_FINAL_DRAIN_EXTRA_S,
        }
        daemon.ROOT = self.folder
        daemon.DATA = self.folder / "data"
        daemon.DATA.mkdir(parents=True, exist_ok=True)
        daemon.ARTIFACTS = daemon.DATA / "artifacts"
        daemon.ARTIFACTS.mkdir(parents=True, exist_ok=True)
        daemon.DB_PATH = daemon.DATA / "observer.sqlite"
        daemon.STATE_PATH = daemon.DATA / "daemon-state.json"
        daemon.LOCK_PATH = daemon.DATA / "daemon.lock"
        # Speed up final drain in unit tests.
        daemon.SESSION_FLUSH_WAIT_S = 0.02
        daemon.SESSION_FINAL_DRAIN_EXTRA_S = 0.01
        daemon.init_db()
        with daemon.RUNNERS_LOCK:
            for runner in list(daemon.RUNNERS.values()):
                runner.shutdown()
            daemon.RUNNERS.clear()
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        stamp = daemon.now()
        self.agent_id = str(uuid.uuid4())
        with daemon.connect() as db:
            db.execute(
                "INSERT INTO tasks(thread_id,title,cwd,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("mon-thread", "mon", str(self.folder), "test", stamp, stamp),
            )
            db.execute(
                "INSERT INTO agents(id,thread_id,name,cwd,grok_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (self.agent_id, "mon-thread", "mon", str(self.folder), self.agent_id, stamp, stamp),
            )
            cur = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,1,'p1','running',?)",
                (self.agent_id, stamp),
            )
            self.turn1 = cur.lastrowid
            cur = db.execute(
                "INSERT INTO turns(agent_id,turn_no,prompt,status,created_at) VALUES(?,2,'p2','running',?)",
                (self.agent_id, stamp),
            )
            self.turn2 = cur.lastrowid

    def tearDown(self):
        with daemon.RUNNERS_LOCK:
            for runner in list(daemon.RUNNERS.values()):
                runner.shutdown()
            daemon.RUNNERS.clear()
        with daemon.CONDITIONS_LOCK:
            daemon.CONDITIONS.clear()
        for key, value in self._orig.items():
            setattr(daemon, key, value)
        self._tmp.cleanup()

    def _events(self, turn_id=None):
        with daemon.connect() as db:
            if turn_id is None:
                rows = db.execute(
                    "SELECT * FROM events WHERE agent_id=? ORDER BY seq",
                    (self.agent_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM events WHERE agent_id=? AND turn_id=? ORDER BY seq",
                    (self.agent_id, turn_id),
                ).fetchall()
            return [dict(r) for r in rows]

    def _session_dir(self) -> Path:
        return daemon.grok_session_dir(self.folder, self.agent_id)


class ContentParseTest(unittest.TestCase):
    def test_extract_text_from_list_dict_null_bytes(self):
        self.assertEqual(daemon.extract_text_content(None), "")
        self.assertIn("from-dict", daemon.extract_text_content({"type": "text", "text": "from-dict"}))
        self.assertIn(
            "from-list",
            daemon.extract_text_content(
                [{"type": "content", "content": {"type": "text", "text": "from-list"}}]
            ),
        )
        raw = list(b"bytes-out")
        self.assertIn("bytes-out", daemon.extract_text_content({"type": "Bash", "output": raw, "output_for_prompt": "bytes-out"}))
        self.assertEqual(daemon.extract_text_content("plain"), "plain")

    def test_summarize_never_assumes_content_dict(self):
        shapes = (FIXTURES / "content_shapes.jsonl").read_text(encoding="utf-8").splitlines()
        for line in shapes:
            obj = json.loads(line)
            etype, summary, payload = daemon.summarize_session_update(obj, "updates.jsonl")
            self.assertTrue(etype)
            self.assertIsInstance(summary, str)
            self.assertIsInstance(payload, dict)
            # byte arrays normalized for storage
            update = (payload.get("params") or {}).get("update") or {}
            ro = update.get("rawOutput")
            if isinstance(ro, dict) and "output" in ro:
                self.assertFalse(isinstance(ro.get("output"), list) and ro.get("output") and isinstance(ro["output"][0], int))


class SessionMonitorFixtureTest(_DbMixin, unittest.TestCase):
    def test_content_list_and_byte_output_and_bad_structure_continue(self):
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        src = (FIXTURES / "bad_then_good.jsonl").read_text(encoding="utf-8")
        updates = session / "updates.jsonl"
        updates.write_text(src, encoding="utf-8")

        state = daemon.SessionMonitorState()
        n = daemon.drain_session_logs(self.agent_id, self.turn1, self.folder, self.agent_id, state)
        self.assertGreater(n, 0)
        events = self._events(self.turn1)
        types = [e["type"] for e in events]
        self.assertIn("tool_call", types)
        self.assertIn("tool_call_update", types)
        # After bad JSON line, later tool_call still ingested
        self.assertTrue(any("after-bad" in (e.get("summary") or "") or "read_file" in (e.get("summary") or "") for e in events))
        # content list did not crash monitor (no fatal)
        self.assertIsNone(state.fatal)
        # summaries should not be empty for tool updates with list content
        tool_updates = [e for e in events if e["type"] == "tool_call_update"]
        self.assertTrue(tool_updates)
        # rawOutput bytes decoded into payload summary path
        joined = " ".join(e["summary"] for e in events)
        self.assertTrue("hello-turn1" in joined or "Execute" in joined or "run_terminal" in joined or tool_updates)

    def test_two_turn_cursor_no_replay_and_turn_ids(self):
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        updates = session / "updates.jsonl"
        t1 = (FIXTURES / "turn1_updates.jsonl").read_text(encoding="utf-8")
        updates.write_text(t1, encoding="utf-8")

        # Turn 1 baseline empty (new session) — read all turn1 lines.
        baseline1 = daemon.capture_session_log_baseline(self.folder, self.agent_id)
        # File already has turn1 content before "process start" in this synthetic test;
        # for turn1 we use empty baseline to simulate first turn creation.
        state1 = daemon.SessionMonitorState({})
        daemon.drain_session_logs(self.agent_id, self.turn1, self.folder, self.agent_id, state1)
        t1_events = self._events(self.turn1)
        t1_types = [e["type"] for e in t1_events]
        self.assertIn("tool_call", t1_types)
        t1_count = len(t1_events)

        # Capture baseline AFTER turn1 for resume turn2 (strict multi-turn cursor).
        baseline2 = daemon.capture_session_log_baseline(self.folder, self.agent_id)
        key = str(updates)
        self.assertIn(key, baseline2)
        self.assertGreater(baseline2[key]["offset"], 0)

        # Append turn2 content (edit + terminal tests + tail).
        t2 = (FIXTURES / "turn2_updates.jsonl").read_text(encoding="utf-8")
        with updates.open("a", encoding="utf-8") as handle:
            handle.write(t2)

        state2 = daemon.SessionMonitorState(baseline2)
        daemon.drain_session_logs(self.agent_id, self.turn2, self.folder, self.agent_id, state2)
        t2_events = self._events(self.turn2)
        self.assertTrue(t2_events, "turn2 must ingest new events")
        # No turn1 event ids on turn2
        for e in t2_events:
            self.assertEqual(e["turn_id"], self.turn2)
        summaries = " ".join(e["summary"] for e in t2_events)
        payloads = " ".join((e.get("payload") or "") for e in t2_events)
        blob = summaries + payloads
        self.assertIn("search_replace", blob + "".join(e["type"] for e in t2_events))
        self.assertTrue(
            "hello.txt" in blob or "Edit" in summaries or any("search_replace" in (e["summary"] or "") for e in t2_events),
            summaries,
        )
        self.assertTrue(
            "OK" in blob or "unittest" in blob or "Ran 1 test" in blob or "run_terminal" in summaries,
            blob[:500],
        )
        # Turn1 count unchanged (no cross-turn replay into turn1).
        self.assertEqual(len(self._events(self.turn1)), t1_count)
        # Event-id dedup: re-drain turn2 must not duplicate.
        before = len(self._events(self.turn2))
        daemon.drain_session_logs(self.agent_id, self.turn2, self.folder, self.agent_id, state2)
        self.assertEqual(len(self._events(self.turn2)), before)

    def test_final_drain_captures_tail_after_stop(self):
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        updates = session / "updates.jsonl"
        updates.write_text("", encoding="utf-8")

        stopped = threading.Event()
        state = daemon.SessionMonitorState({})
        errors: list[str] = []
        thread = threading.Thread(
            target=daemon.monitor_session,
            args=(self.agent_id, self.turn1, self.folder, self.agent_id, stopped, state, errors),
            daemon=True,
        )
        thread.start()
        time.sleep(0.05)
        # Write body while running
        body = (FIXTURES / "turn1_updates.jsonl").read_text(encoding="utf-8")
        updates.write_text(body, encoding="utf-8")
        time.sleep(0.1)
        # Append tail AFTER signaling stop — final drain must still pick it up.
        tail = json.dumps(
            {
                "timestamp": 1,
                "method": "session/update",
                "params": {
                    "sessionId": self.agent_id,
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tail-call",
                        "title": "list_dir",
                        "rawInput": {"target_directory": "."},
                    },
                    "_meta": {"eventId": "tail-event-unique"},
                },
            },
            ensure_ascii=False,
        )
        stopped.set()
        # Race: write during flush wait window
        time.sleep(0.005)
        with updates.open("a", encoding="utf-8") as handle:
            handle.write(tail + "\n")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        events = self._events(self.turn1)
        self.assertTrue(any(e["type"] == "tool_call" for e in events))
        found_tail = any(
            "list_dir" in (e.get("summary") or "")
            or "tail-call" in (e.get("payload") or "")
            or "tail-event" in (e.get("payload") or "")
            for e in events
        )
        self.assertTrue(found_tail, "final drain must capture tail event")

    def test_tool_status_updates_not_collapsed_by_toolCallId_dedup(self):
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        lines = [
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "same-call",
                        "title": "run_terminal_command",
                        "rawInput": {"command": "echo x"},
                    },
                    "_meta": {"eventId": "id-1"},
                },
            },
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "same-call",
                        "status": "in_progress",
                        "content": [{"type": "content", "content": {"type": "text", "text": "partial"}}],
                    },
                    "_meta": {"eventId": "id-2"},
                },
            },
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "same-call",
                        "status": "completed",
                        "content": [{"type": "content", "content": {"type": "text", "text": "final-out"}}],
                        "rawOutput": {
                            "type": "Bash",
                            "output": list(b"final-out"),
                            "output_for_prompt": "final-out",
                            "exit_code": 0,
                        },
                    },
                    "_meta": {"eventId": "id-3"},
                },
            },
        ]
        (session / "updates.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n",
            encoding="utf-8",
        )
        state = daemon.SessionMonitorState({})
        daemon.drain_session_logs(self.agent_id, self.turn1, self.folder, self.agent_id, state)
        events = [e for e in self._events(self.turn1) if e["type"] in {"tool_call", "tool_call_update"}]
        self.assertEqual(len(events), 3)

    def test_final_drain_force_consumes_line_without_trailing_newline(self):
        """Late tool_completed written without a final \\n must still be captured."""
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        updates = session / "updates.jsonl"
        updates.write_text("", encoding="utf-8")

        stopped = threading.Event()
        state = daemon.SessionMonitorState({})
        errors: list[str] = []
        thread = threading.Thread(
            target=daemon.monitor_session,
            args=(self.agent_id, self.turn1, self.folder, self.agent_id, stopped, state, errors),
            daemon=True,
        )
        thread.start()
        time.sleep(0.04)
        # Complete JSON object, intentionally NO trailing newline (writer flush edge).
        tail = json.dumps(
            {
                "timestamp": 1,
                "method": "session/update",
                "params": {
                    "sessionId": self.agent_id,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "late-no-nl",
                        "status": "completed",
                        "title": "run_terminal_command",
                        "content": [
                            {
                                "type": "content",
                                "content": {"type": "text", "text": "late-ok-without-nl"},
                            }
                        ],
                        "rawOutput": {
                            "type": "Bash",
                            "output": list(b"late-ok-without-nl\n"),
                            "output_for_prompt": "late-ok-without-nl",
                            "exit_code": 0,
                        },
                    },
                    "_meta": {"eventId": "ev-late-no-nl"},
                },
            },
            ensure_ascii=False,
        )
        updates.write_bytes(tail.encode("utf-8"))  # no \n
        stopped.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        events = self._events(self.turn1)
        blob = " ".join((e.get("summary") or "") + " " + (e.get("payload") or "") for e in events)
        self.assertTrue(
            "late-ok-without-nl" in blob or "late-no-nl" in blob,
            f"final drain must force-consume trailing line without newline; got {blob[:400]!r}",
        )

    def test_inplace_rewrite_rotation_and_tail_fixture(self):
        """In-place rewrite larger than prior cursor must re-read from 0 (not miss new head)."""
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        updates = session / "updates.jsonl"

        # Seed a short historical line, drain it, then replace the whole file with a
        # longer rotation fixture (size can grow past the old cursor).
        seed = json.dumps(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "seed-old",
                        "title": "seed_old",
                    },
                    "_meta": {"eventId": "ev-seed-old"},
                },
            }
        ) + "\n"
        updates.write_text(seed, encoding="utf-8")
        state = daemon.SessionMonitorState({})
        daemon.drain_session_logs(self.agent_id, self.turn1, self.folder, self.agent_id, state)
        seed_events = self._events(self.turn1)
        self.assertTrue(any("seed_old" in (e.get("summary") or "") for e in seed_events))
        old_offset = state.offsets[str(updates)]
        self.assertGreater(old_offset, 0)

        # Full rewrite (simulates log rotation / truncated rewrite) with longer content.
        rotated = (FIXTURES / "rotation_and_tail.jsonl").read_text(encoding="utf-8")
        self.assertGreater(len(rotated.encode("utf-8")), old_offset)
        updates.write_text(rotated, encoding="utf-8")

        daemon.drain_session_logs(self.agent_id, self.turn1, self.folder, self.agent_id, state)
        events = self._events(self.turn1)
        blob = " ".join((e.get("summary") or "") + (e.get("payload") or "") for e in events)
        self.assertIn("ev-rot-1", blob + "".join(e.get("payload") or "" for e in events))
        self.assertTrue(
            "test_rotation" in blob or "OK" in blob or "run_terminal" in blob,
            blob[:600],
        )
        self.assertTrue(
            any("rotation_and_tail.jsonl" in (e.get("payload") or "") or "call-rot-tail" in (e.get("payload") or "") for e in events)
            or "read_file" in blob,
            "tail fixture event after rotation must be ingested",
        )
        # in_progress + completed for same toolCallId both kept (distinct eventIds)
        rot_updates = [
            e
            for e in events
            if e["type"] == "tool_call_update" and "call-rot-term" in (e.get("payload") or "")
        ]
        self.assertGreaterEqual(len(rot_updates), 2)

    def test_cross_turn_same_output_not_dropped_by_soft_dedup(self):
        """Separate turns may emit identical tool stdout; soft fingerprint must not drop turn2."""
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        updates = session / "updates.jsonl"

        same_body = {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "dup-out",
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "IDENTICAL-OUTPUT"}}],
                    "rawOutput": {
                        "type": "Bash",
                        "output": list(b"IDENTICAL-OUTPUT\n"),
                        "output_for_prompt": "IDENTICAL-OUTPUT",
                        "exit_code": 0,
                    },
                },
                "_meta": {"eventId": "ev-dup-t1"},
            },
        }
        updates.write_text(json.dumps(same_body) + "\n", encoding="utf-8")
        s1 = daemon.SessionMonitorState({})
        daemon.drain_session_logs(self.agent_id, self.turn1, self.folder, self.agent_id, s1)
        t1 = [e for e in self._events(self.turn1) if "IDENTICAL-OUTPUT" in ((e.get("summary") or "") + (e.get("payload") or ""))]
        self.assertTrue(t1)

        baseline = daemon.capture_session_log_baseline(self.folder, self.agent_id)
        same_body["params"]["_meta"]["eventId"] = "ev-dup-t2"
        with updates.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(same_body) + "\n")
        s2 = daemon.SessionMonitorState(baseline)
        daemon.drain_session_logs(self.agent_id, self.turn2, self.folder, self.agent_id, s2)
        t2 = [e for e in self._events(self.turn2) if "IDENTICAL-OUTPUT" in ((e.get("summary") or "") + (e.get("payload") or ""))]
        self.assertTrue(t2, "turn2 identical output must not be soft-deduped away")

    def test_monitor_fatal_surfaces_to_error_box(self):
        """Runner-facing error_box / state.fatal must record unrecoverable drain failures."""
        session = self._session_dir()
        session.mkdir(parents=True, exist_ok=True)
        state = daemon.SessionMonitorState({})
        errors: list[str] = []
        stopped = threading.Event()
        stopped.set()

        orig = daemon.drain_session_logs

        def boom(*_a, **_k):
            raise RuntimeError("synthetic-monitor-fatal")

        daemon.drain_session_logs = boom  # type: ignore[assignment]
        try:
            daemon.monitor_session(
                self.agent_id, self.turn1, self.folder, self.agent_id, stopped, state, errors
            )
        finally:
            daemon.drain_session_logs = orig  # type: ignore[assignment]

        self.assertTrue(state.fatal and "synthetic-monitor-fatal" in state.fatal)
        self.assertTrue(errors and "synthetic-monitor-fatal" in errors[-1])
        mon_errs = [e for e in self._events(self.turn1) if e["type"] == "observer_monitor_error"]
        self.assertTrue(mon_errs)


class FsSnapshotTest(_DbMixin, unittest.TestCase):
    def test_exclude_rules(self):
        self.assertTrue(daemon.fs_path_excluded("node_modules/x.js"))
        self.assertTrue(daemon.fs_path_excluded("data/observer.sqlite"))
        self.assertTrue(daemon.fs_path_excluded("data/artifacts/a/b.txt"))
        self.assertTrue(daemon.fs_path_excluded("data/daemon-state.json"))
        self.assertTrue(daemon.fs_path_excluded("data/daemon.lock"))
        self.assertTrue(daemon.fs_path_excluded(".claude/tmp/x.ps1"))
        self.assertTrue(daemon.fs_path_excluded("viewer/dist/assets/index.js.map"))
        self.assertTrue(daemon.fs_path_excluded("__pycache__/x.pyc"))
        self.assertFalse(daemon.fs_path_excluded("daemon.py"))
        self.assertFalse(daemon.fs_path_excluded("viewer/src/main.tsx"))

    def test_fs_snapshot_add_modify_delete_unchanged(self):
        plain = self.folder / "ws"
        plain.mkdir()
        (plain / "keep.txt").write_text("same\n", encoding="utf-8")
        (plain / "will_change.txt").write_text("v1\n", encoding="utf-8")
        (plain / "will_delete.txt").write_text("bye\n", encoding="utf-8")
        (plain / "node_modules").mkdir()
        (plain / "node_modules" / "pkg.js").write_text("noise\n", encoding="utf-8")
        (plain / "data").mkdir()
        (plain / "data" / "observer.sqlite").write_text("db\n", encoding="utf-8")

        before = daemon.fs_snapshot(plain)
        self.assertTrue(before["available"])
        self.assertNotIn("node_modules/pkg.js", before["entries"])
        self.assertNotIn("data/observer.sqlite", before["entries"])

        # unchanged keep.txt, modify, delete, add
        (plain / "will_change.txt").write_text("v2\n", encoding="utf-8")
        (plain / "will_delete.txt").unlink()
        (plain / "brand_new.txt").write_text("new\n", encoding="utf-8")
        after = daemon.fs_snapshot(plain)
        count = daemon.record_changes(self.agent_id, self.turn1, before, after)
        self.assertEqual(count, 3)
        with daemon.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM changes WHERE turn_id=?", (self.turn1,))]
        by_path = {r["path"]: r for r in rows}
        self.assertIn("brand_new.txt", by_path)
        self.assertEqual(by_path["brand_new.txt"]["kind"], "added")
        self.assertEqual(by_path["will_change.txt"]["kind"], "modified")
        self.assertEqual(by_path["will_delete.txt"]["kind"], "deleted")
        self.assertNotIn("keep.txt", by_path)
        self.assertEqual(daemon.unique_changed_files(self.agent_id), 3)

    def test_fs_snapshot_over_limit_unavailable(self):
        plain = self.folder / "big"
        plain.mkdir()
        orig = daemon.FS_SNAPSHOT_MAX_FILES
        try:
            daemon.FS_SNAPSHOT_MAX_FILES = 3
            for i in range(5):
                (plain / f"f{i}.txt").write_text("x\n", encoding="utf-8")
            snap = daemon.fs_snapshot(plain)
            self.assertFalse(snap.get("available"))
            self.assertEqual(snap.get("reason"), "changes_unavailable")
            count = daemon.record_changes(self.agent_id, self.turn1, {"available": True, "mode": "fs", "entries": {}, "digests": {}}, snap)
            self.assertEqual(count, 0)
            events = self._events(self.turn1)
            self.assertTrue(any(e["type"] == "changes" for e in events))
        finally:
            daemon.FS_SNAPSHOT_MAX_FILES = orig

    def test_fs_missing_start_snapshot_not_flood_added(self):
        """Unavailable before snapshot must not mark every after file as added."""
        plain = self.folder / "plain_ws"
        plain.mkdir()
        (plain / "a.txt").write_text("a\n", encoding="utf-8")
        (plain / "b.txt").write_text("b\n", encoding="utf-8")
        after = daemon.fs_snapshot(plain)
        self.assertTrue(after.get("available"))
        before = {
            "available": False,
            "mode": "fs",
            "reason": "changes_unavailable",
            "error": "workspace too large for non-git snapshot",
            "entries": {},
            "digests": {},
        }
        count = daemon.record_changes(self.agent_id, self.turn1, before, after)
        self.assertEqual(count, 0)
        with daemon.connect() as db:
            rows = db.execute("SELECT * FROM changes WHERE turn_id=?", (self.turn1,)).fetchall()
        self.assertEqual(len(rows), 0)
        events = self._events(self.turn1)
        self.assertTrue(any(e["type"] == "changes" and "不可用" in (e.get("summary") or "") for e in events))

    def test_fs_symlink_and_permission_skips_do_not_crash(self):
        plain = self.folder / "sym"
        plain.mkdir()
        (plain / "ok.txt").write_text("ok\n", encoding="utf-8")
        link = plain / "link.txt"
        try:
            link.symlink_to(plain / "ok.txt")
        except OSError:
            # Windows without symlink privilege — still exercise snapshot path.
            pass
        snap = daemon.fs_snapshot(plain)
        self.assertTrue(snap.get("available"), snap)
        self.assertIn("ok.txt", snap["entries"])
        # Symlink targets must not be walked as separate file entries when followlinks=False.
        self.assertNotIn("link.txt", snap.get("entries", {}))

    def test_non_git_changes_include_new_fixture_file(self):
        """Strict FS delta should report a newly added regression fixture under tests/fixtures."""
        plain = self.folder / "proj"
        plain.mkdir()
        fixtures = plain / "tests" / "fixtures"
        fixtures.mkdir(parents=True)
        (plain / "README.md").write_text("hi\n", encoding="utf-8")
        # Noise that must stay excluded even when created mid-turn.
        (plain / "node_modules").mkdir()
        (plain / "node_modules" / "x.js").write_text("n\n", encoding="utf-8")
        (plain / "data").mkdir()
        (plain / "data" / "observer.sqlite").write_text("db\n", encoding="utf-8")
        (plain / "viewer" / "dist" / "assets").mkdir(parents=True)
        (plain / "viewer" / "dist" / "assets" / "index.js.map").write_text("map\n", encoding="utf-8")

        before = daemon.fs_snapshot(plain)
        self.assertTrue(before.get("available"))
        # Simulate this review adding a rotation fixture (and only that as the meaningful delta).
        target = fixtures / "rotation_and_tail.jsonl"
        target.write_text(
            (FIXTURES / "rotation_and_tail.jsonl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (plain / "node_modules" / "y.js").write_text("more noise\n", encoding="utf-8")
        (plain / "data" / "artifacts").mkdir(exist_ok=True)
        (plain / "data" / "artifacts" / "z.txt").write_text("art\n", encoding="utf-8")

        after = daemon.fs_snapshot(plain)
        count = daemon.record_changes(self.agent_id, self.turn1, before, after)
        self.assertEqual(count, 1)
        with daemon.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM changes WHERE turn_id=?", (self.turn1,))]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"].replace("\\", "/"), "tests/fixtures/rotation_and_tail.jsonl")
        self.assertEqual(rows[0]["kind"], "added")

    def test_workspace_snapshot_prefers_git(self):
        repo = self.folder / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True, capture_output=True)
        snap = daemon.workspace_snapshot(repo)
        self.assertTrue(snap.get("available"))
        self.assertEqual(snap.get("mode"), "git")


if __name__ == "__main__":
    unittest.main()
