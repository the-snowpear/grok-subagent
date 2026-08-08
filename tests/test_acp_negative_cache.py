"""Regression tests for the in-process ACP negative cache.

Covers the observer behavior decided for real grok 1.0.0: the agent stdio
runtime does not expose ``x.ai/session/info`` (JSON-RPC -32601). Only that
definitive error is negative-cached, in memory, keyed by the resolved grok
executable identity; every other failure mode is never cached.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import grok_acp_context
from grok_acp_context import AcpProbeError, AcpUnsupportedCache, probe_is_unsupported


IDENTITY_A = ("C:/Grok/bin/grok.exe", "1.0.0", 123_456, 1_700_000_000_000_000_000)
IDENTITY_B = ("C:/Grok/bin/grok.exe", "1.0.1", 123_999, 1_700_000_000_000_000_001)
IDENTITY_C = ("D:/other/grok.exe", "1.0.0", 99_999, 1_700_000_000_000_000_002)
METHOD = "x.ai/session/info"


def sample_info():
    return {
        "sessionId": "sess-1",
        "cwd": "/tmp/project",
        "agentName": "grok-build",
        "model": "grok-build",
        "modelDisplayName": "Grok Build",
        "context": {
            "used": 36_700,
            "total": 1_000_000,
            "systemPromptTokens": 1_200,
            "toolDefinitionsCount": 12,
            "toolDefinitionsTokens": 5_600,
            "messageCount": 18,
            "messageTokens": 29_900,
            "freeTokens": 963_300,
            "usagePct": 4,
            "compactionCount": 1,
            "turnCount": 5,
            "toolCallCount": 12,
            "autoCompactThresholdPercent": 85,
            "usageCategories": [
                {"label": "Skills", "tokens": 2_400, "detail": "21 skills"},
                {"label": "MCP servers", "tokens": 320, "detail": "4 servers"},
            ],
        },
    }


class _FakeStdin:
    def write(self, text: str) -> None:
        self.written = text

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        yield from self._lines


class _FakeProc:
    """Minimal subprocess.Popen stand-in for _JsonRpcStdio tests."""

    def __init__(self, lines=()):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_called = True
        self.returncode = 1

    def kill(self):
        self.kill_called = True
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode


class AcpProbeErrorTest(unittest.TestCase):
    def test_keeps_structured_code_method_data_and_message(self):
        err = AcpProbeError(
            "ACP x.ai/session/info failed: Method not found",
            code=-32601,
            method=METHOD,
            data={"detail": "not implemented"},
        )
        self.assertEqual(err.code, -32601)
        self.assertEqual(err.method, METHOD)
        self.assertEqual(err.data, {"detail": "not implemented"})
        self.assertIn("Method not found", str(err))
        self.assertIsInstance(err, RuntimeError)

    def test_defaults_are_none(self):
        err = AcpProbeError("boom")
        self.assertIsNone(err.code)
        self.assertIsNone(err.method)
        self.assertIsNone(err.data)

    def test_jsonrpc_error_response_populates_fields_and_child_terminated(self):
        # The real request() error path must attach the structured error and
        # still terminate the ACP child via close().
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found", "data": {"detail": "nope"}},
        }
        fake = _FakeProc([json.dumps(response)])
        with mock.patch.object(grok_acp_context.subprocess, "Popen", return_value=fake):
            rpc = grok_acp_context._JsonRpcStdio(Path("."), {}, timeout=2.0)
            with self.assertRaises(AcpProbeError) as ctx:
                rpc.request(METHOD, {"sessionId": "sess-1"})
            err = ctx.exception
            self.assertEqual(err.code, -32601)
            self.assertEqual(err.method, METHOD)
            self.assertEqual(err.data, {"detail": "nope"})
            rpc.close()
        self.assertTrue(fake.terminate_called, "ACP child must be terminated on probe close")

    def test_probe_is_unsupported_requires_both_code_and_method(self):
        base = dict(code=-32601, method=METHOD)
        self.assertTrue(probe_is_unsupported(AcpProbeError("x", **base)))
        self.assertFalse(probe_is_unsupported(AcpProbeError("x", code=-32601, method="session/load")))
        self.assertFalse(probe_is_unsupported(AcpProbeError("x", code=-32000, method=METHOD)))
        self.assertFalse(probe_is_unsupported(AcpProbeError("x")))
        self.assertFalse(probe_is_unsupported(OSError("nope")))


class UnsupportedCacheTest(unittest.TestCase):
    def setUp(self):
        grok_acp_context.acp_unsupported_cache.clear()

    def test_record_and_query_are_thread_safe_set_semantics(self):
        cache = AcpUnsupportedCache()
        self.assertEqual(len(cache), 0)
        self.assertTrue(cache.record_unsupported(IDENTITY_A, METHOD))
        # Duplicate record returns False (no new entry) and does not grow.
        self.assertFalse(cache.record_unsupported(IDENTITY_A, METHOD))
        self.assertEqual(len(cache), 1)
        self.assertTrue(cache.is_unsupported(IDENTITY_A, METHOD))
        self.assertFalse(cache.is_unsupported(IDENTITY_B, METHOD))
        self.assertFalse(cache.is_unsupported(IDENTITY_A, "session/load"))

    def test_fresh_instance_starts_empty_like_new_process(self):
        recorded = AcpUnsupportedCache()
        recorded.record_unsupported(IDENTITY_A, METHOD)
        fresh = AcpUnsupportedCache()  # simulates an observer process restart
        self.assertFalse(fresh.is_unsupported(IDENTITY_A, METHOD))
        self.assertEqual(len(fresh), 0)

    def test_fresh_module_instance_starts_empty_like_new_process(self):
        # Simulate an observer process restart: a brand-new module instance
        # (loaded under a distinct name so the shared module is untouched) gets
        # an empty negative cache even though this process already recorded one.
        grok_acp_context.acp_unsupported_cache.record_unsupported(IDENTITY_A, METHOD)
        spec = importlib.util.spec_from_file_location(
            "grok_acp_context_fresh", grok_acp_context.__file__
        )
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        try:
            self.assertEqual(len(fresh.acp_unsupported_cache), 0)
            self.assertFalse(fresh.acp_unsupported_cache.is_unsupported(IDENTITY_A, METHOD))
        finally:
            sys.modules.pop("grok_acp_context_fresh", None)
            grok_acp_context.acp_unsupported_cache.clear()

    def test_identity_change_is_a_miss(self):
        cache = AcpUnsupportedCache()
        cache.record_unsupported(IDENTITY_A, METHOD)
        self.assertFalse(cache.is_unsupported(IDENTITY_B, METHOD), "version/size/mtime change must miss")
        self.assertFalse(cache.is_unsupported(IDENTITY_C, METHOD), "path change must miss")


class GrokBinaryIdentityTest(unittest.TestCase):
    def tearDown(self):
        with grok_acp_context._identity_cache_lock:
            grok_acp_context._identity_cache.clear()
        with grok_acp_context._version_memo_lock:
            grok_acp_context._version_memo.clear()

    def test_identity_reused_via_cheap_stat_and_refreshed_on_fingerprint_change(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "grok.exe"
            exe.write_bytes(b"x" * 100)
            with (
                mock.patch.object(grok_acp_context.shutil, "which", return_value=str(exe)),
                mock.patch.object(grok_acp_context, "_run_grok_version", return_value="1.0.0") as version,
            ):
                identity = grok_acp_context.grok_binary_identity()
                self.assertEqual(identity[0], str(exe.resolve()))
                self.assertEqual(identity[1], "1.0.0")
                self.assertEqual(identity[2], 100)
                version.assert_called_once()

                # Unchanged fingerprint: reused from the identity cache, no
                # fresh version lookup (cheap path/stat per turn).
                again = grok_acp_context.grok_binary_identity()
                self.assertEqual(again, identity)
                version.assert_called_once()

                # resolve_version=False never spawns the version lookup when
                # the cached fingerprint is still valid.
                fast = grok_acp_context.grok_binary_identity(resolve_version=False)
                self.assertEqual(fast, identity)
                version.assert_called_once()

                # Fingerprint change -> stale fast path defers to the worker.
                exe.write_bytes(b"y" * 101)
                self.assertIsNone(grok_acp_context.grok_binary_identity(resolve_version=False))
                refreshed = grok_acp_context.grok_binary_identity()
                self.assertEqual(refreshed[2], 101)
                self.assertNotEqual(refreshed, identity)
                self.assertEqual(version.call_count, 2)

    def test_returns_none_when_grok_not_resolvable(self):
        with mock.patch.object(grok_acp_context.shutil, "which", return_value=None):
            self.assertIsNone(grok_acp_context.grok_binary_identity())


def _load_live_module():
    """Import live_streaming against a fake daemon that records events."""
    calls = []
    fake = types.ModuleType("daemon")

    def add_event(*args):
        calls.append(args)
        return len(calls)

    fake.add_event = add_event
    fake.SESSION_SKIP_TYPES = frozenset(
        {"agent_message_chunk", "agent_thought_chunk", "phase_changed"}
    )
    fake.main = lambda: None
    fake.system_proxy_environment = lambda base: (dict(base), None)
    sys.modules["daemon"] = fake
    sys.modules.pop("live_streaming", None)
    module = importlib.import_module("live_streaming")
    return module, fake, calls


class LiveStreamingNegativeCacheTest(unittest.TestCase):
    def setUp(self):
        grok_acp_context.acp_unsupported_cache.clear()
        os.environ.pop("GROK_OBSERVER_FAKE_GROK", None)
        os.environ.pop("GROK_OBSERVER_CONTEXT_DEBUG", None)
        os.environ.pop("GROK_OBSERVER_CONTEXT_TELEMETRY", None)

    def tearDown(self):
        sys.modules.pop("live_streaming", None)
        sys.modules.pop("daemon", None)
        grok_acp_context.acp_unsupported_cache.clear()
        os.environ.pop("GROK_OBSERVER_FAKE_GROK", None)
        os.environ.pop("GROK_OBSERVER_CONTEXT_DEBUG", None)
        os.environ.pop("GROK_OBSERVER_CONTEXT_TELEMETRY", None)

    def test_session_info_32601_records_negative_cache(self):
        module, _, _ = _load_live_module()
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(
                module,
                "probe_session_context",
                side_effect=AcpProbeError(
                    "ACP x.ai/session/info failed: Method not found",
                    code=-32601,
                    method=METHOD,
                    data={},
                ),
            ) as probe,
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            module._probe_context_worker("agent-1", 1)
            self.assertEqual(probe.call_count, 1)
        self.assertTrue(grok_acp_context.acp_unsupported_cache.is_unsupported(IDENTITY_A, METHOD))

    def test_32601_records_only_pre_probe_identity_not_reparsed(self):
        # TOCTOU regression: the binary is replaced (identity A -> B) while the
        # probe is in flight. The -32601 must be attributed to the identity
        # resolved *before* the probe started (A), never to a re-resolved one
        # (B). B must stay a miss so the next turn re-probes. A re-resolving
        # implementation consumes the second side_effect value and caches B,
        # which fails this test.
        module, _, _ = _load_live_module()
        with (
            mock.patch.object(
                module,
                "grok_binary_identity",
                side_effect=[IDENTITY_A, IDENTITY_B],
            ) as identity,
            mock.patch.object(
                module,
                "probe_session_context",
                side_effect=AcpProbeError(
                    "ACP x.ai/session/info failed: Method not found",
                    code=-32601,
                    method=METHOD,
                    data={},
                ),
            ),
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            module._probe_context_worker("agent-1", 1)
            identity.assert_called_once()  # resolved exactly once, pre-probe
        cache = grok_acp_context.acp_unsupported_cache
        self.assertTrue(cache.is_unsupported(IDENTITY_A, METHOD))
        self.assertFalse(cache.is_unsupported(IDENTITY_B, METHOD), "new binary must stay a miss")

    def test_32601_with_none_identity_fails_open_no_cache(self):
        module, _, _ = _load_live_module()
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=None),
            mock.patch.object(
                module,
                "probe_session_context",
                side_effect=AcpProbeError(
                    "ACP x.ai/session/info failed: Method not found",
                    code=-32601,
                    method=METHOD,
                    data={},
                ),
            ),
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            module._probe_context_worker("agent-1", 1)
        self.assertEqual(len(grok_acp_context.acp_unsupported_cache), 0)

    def test_debug_logs_only_on_first_mark_not_on_cache_hit(self):
        module, _, _ = _load_live_module()
        os.environ["GROK_OBSERVER_CONTEXT_DEBUG"] = "1"
        err = AcpProbeError(
            "ACP x.ai/session/info failed: Method not found", code=-32601, method=METHOD
        )
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(module, "probe_session_context", side_effect=err) as probe,
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                module._probe_context_worker("agent-1", 1)  # first probe -> mark + one line
                module._probe_context_worker("agent-2", 2)  # cache hit -> skip, no spam
            self.assertEqual(probe.call_count, 1)
        lines = stderr.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("negative-cached", lines[0])

    def test_cache_hit_skips_probe_and_popen(self):
        module, _, _ = _load_live_module()
        grok_acp_context.acp_unsupported_cache.record_unsupported(IDENTITY_A, METHOD)
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(
                module, "probe_session_context", side_effect=AssertionError("probe must not run")
            ) as probe,
            mock.patch.object(
                module, "_context_probe_target", side_effect=AssertionError("target must not be read")
            ) as target,
            mock.patch.object(
                grok_acp_context.subprocess, "Popen", side_effect=AssertionError("no Popen on skip")
            ) as popen,
        ):
            module._probe_context_worker("agent-1", 1)
            probe.assert_not_called()
            target.assert_not_called()
            popen.assert_not_called()

    def test_scheduler_skips_thread_on_cache_hit(self):
        module, _, _ = _load_live_module()
        grok_acp_context.acp_unsupported_cache.record_unsupported(IDENTITY_A, METHOD)
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(
                module.threading, "Thread", side_effect=AssertionError("no worker thread on hit")
            ),
        ):
            module._schedule_context_probe("agent-1", 1)  # must return without spawning

    def test_scheduler_spawns_worker_when_not_cached(self):
        module, _, _ = _load_live_module()
        spawned = []

        class _FakeThread:
            def __init__(self, *args, **kwargs):
                spawned.append(kwargs)

            def start(self):
                pass

        with mock.patch.object(module.threading, "Thread", _FakeThread):
            module._schedule_context_probe("agent-1", 1)
        self.assertEqual(len(spawned), 1)
        self.assertIn("context-", spawned[0]["name"])

    def test_identity_change_reprobes_and_records_new_entry(self):
        module, _, _ = _load_live_module()
        grok_acp_context.acp_unsupported_cache.record_unsupported(IDENTITY_A, METHOD)
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_B),
            mock.patch.object(
                module,
                "probe_session_context",
                side_effect=AcpProbeError(
                    "ACP x.ai/session/info failed: Method not found",
                    code=-32601,
                    method=METHOD,
                    data={},
                ),
            ) as probe,
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            module._probe_context_worker("agent-1", 1)
            self.assertEqual(probe.call_count, 1)
        self.assertTrue(grok_acp_context.acp_unsupported_cache.is_unsupported(IDENTITY_B, METHOD))
        self.assertTrue(grok_acp_context.acp_unsupported_cache.is_unsupported(IDENTITY_A, METHOD))

    def test_cleared_cache_reprobes_like_observer_restart(self):
        module, _, _ = _load_live_module()
        grok_acp_context.acp_unsupported_cache.record_unsupported(IDENTITY_A, METHOD)
        grok_acp_context.acp_unsupported_cache.clear()  # simulates a process restart
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(
                module,
                "probe_session_context",
                side_effect=AcpProbeError(
                    "ACP x.ai/session/info failed: Method not found",
                    code=-32601,
                    method=METHOD,
                    data={},
                ),
            ) as probe,
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            module._probe_context_worker("agent-1", 1)
            self.assertEqual(probe.call_count, 1)

    def _run_failure_case(self, module, exc):
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(module, "probe_session_context", side_effect=exc) as probe,
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            module._probe_context_worker("agent-1", 1)
            self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(grok_acp_context.acp_unsupported_cache), 0)

    def test_non_32601_errors_never_cached(self):
        module, _, _ = _load_live_module()
        cases = (
            AcpProbeError("ACP x.ai/session/info timed out"),  # timeout
            AcpProbeError("ACP authenticate failed: auth rejected", code=-32000, method="authenticate"),
            AcpProbeError("ACP x.ai/session/info failed: invalid params", code=-32602, method=METHOD),
            AcpProbeError("ACP session/load failed: Method not found", code=-32601, method="session/load"),
            OSError("network down"),
            ValueError("bad value"),
            RuntimeError("boom"),
        )
        for exc in cases:
            with self.subTest(error=exc):
                grok_acp_context.acp_unsupported_cache.clear()
                self._run_failure_case(module, exc)

    def test_popen_start_failure_never_cached(self):
        module, _, _ = _load_live_module()
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
            mock.patch.object(grok_acp_context.subprocess, "Popen", side_effect=OSError("no grok")),
        ):
            # Real probe_session_context path: Popen OSError -> AcpProbeError.
            module._probe_context_worker("agent-1", 1)
        self.assertEqual(len(grok_acp_context.acp_unsupported_cache), 0)

    def test_probe_uses_exact_resolved_executable_from_identity(self):
        module, _, _ = _load_live_module()
        captured = {}

        def fake_probe(session_id, cwd, **kwargs):
            captured.update(kwargs)
            return sample_info()

        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(module, "probe_session_context", side_effect=fake_probe) as probe,
            mock.patch.object(
                module, "_context_probe_target", return_value=("C:/work", "sess-1")
            ),
        ):
            module._probe_context_worker("agent-1", 1)
            self.assertEqual(probe.call_count, 1)
        self.assertEqual(captured["executable"], IDENTITY_A[0])

    def test_jsonrpc_stdio_popens_identity_path(self):
        fake = _FakeProc()
        with mock.patch.object(grok_acp_context.subprocess, "Popen", return_value=fake) as popen:
            rpc = grok_acp_context._JsonRpcStdio(Path("."), {}, executable=IDENTITY_A[0])
            rpc.close()
        args = popen.call_args.args[0]
        self.assertEqual(args, [IDENTITY_A[0], "agent", "--always-approve", "stdio"])

    def test_success_still_emits_context_usage(self):
        module, _, calls = _load_live_module()
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(module, "probe_session_context", return_value=sample_info()),
            mock.patch.object(
                module,
                "_context_probe_target",
                side_effect=[("C:/work", "sess-1"), ("C:/work", "sess-1")],
            ),
        ):
            module._probe_context_worker("agent-1", 1)
        emitted = [call for call in calls if call[2] == "context_usage"]
        self.assertEqual(len(emitted), 1)
        payload = emitted[0][4]
        self.assertEqual(payload["context"]["used"], 36_700)
        self.assertIn("36.7k", emitted[0][3])

    def test_stale_turn_still_drops_context_snapshot(self):
        module, _, calls = _load_live_module()
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(module, "probe_session_context", return_value=sample_info()),
            mock.patch.object(
                module,
                "_context_probe_target",
                side_effect=[("C:/work", "sess-1"), None],  # follow-up turn started mid-probe
            ),
        ):
            module._probe_context_worker("agent-1", 1)
        self.assertFalse([call for call in calls if call[2] == "context_usage"])

    def test_follow_up_running_turn_still_skips_probe(self):
        module, _, calls = _load_live_module()
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(
                module, "probe_session_context", side_effect=AssertionError("must not probe")
            ) as probe,
            mock.patch.object(module, "_context_probe_target", return_value=None),
        ):
            module._probe_context_worker("agent-1", 1)
            probe.assert_not_called()
        self.assertEqual(calls, [])

    def test_telemetry_failure_does_not_change_token_totals(self):
        module, _, calls = _load_live_module()
        with (
            mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
            mock.patch.object(module, "probe_session_context", side_effect=AcpProbeError("timeout")),
            mock.patch.object(module, "_context_probe_target", return_value=("C:/work", "sess-1")),
        ):
            module._probe_context_worker("agent-1", 1)
        # The probe path only ever emits context_usage; a failure emits nothing
        # and cannot touch usage/text accounting.
        self.assertEqual(calls, [])

    def test_no_disk_cache_files_created(self):
        module, _, _ = _load_live_module()
        with tempfile.TemporaryDirectory() as folder:
            with (
                mock.patch.object(module, "grok_binary_identity", return_value=IDENTITY_A),
                mock.patch.object(
                    module,
                    "probe_session_context",
                    side_effect=AcpProbeError(
                        "ACP x.ai/session/info failed: Method not found",
                        code=-32601,
                        method=METHOD,
                        data={},
                    ),
                ),
                mock.patch.object(module, "_context_probe_target", return_value=(folder, "sess-1")),
            ):
                module._probe_context_worker("agent-1", 1)
            self.assertTrue(grok_acp_context.acp_unsupported_cache.is_unsupported(IDENTITY_A, METHOD))
            self.assertEqual(list(Path(folder).iterdir()), [])
            self.assertFalse((Path(folder) / "acp-capability-cache.json").exists())
            self.assertFalse((Path(folder) / "data" / "acp-capability-cache.json").exists())


if __name__ == "__main__":
    unittest.main()
