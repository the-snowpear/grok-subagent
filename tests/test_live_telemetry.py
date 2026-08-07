from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

from context_telemetry import emit_context_snapshot, normalize_context_info
from grok_acp_context import AcpProbeError, _pick_auth_method, context_summary, probe_session_context, unwrap_session_info


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


class ContextTelemetryTest(unittest.TestCase):
    def test_normalize_preserves_semantic_breakdown(self):
        payload = normalize_context_info(sample_info())
        ctx = payload["context"]
        self.assertEqual(payload["model_display_name"], "Grok Build")
        self.assertEqual(ctx["used"], 36_700)
        self.assertEqual(ctx["total"], 1_000_000)
        self.assertEqual(ctx["system_prompt_tokens"], 1_200)
        self.assertEqual(ctx["message_tokens"], 29_900)
        self.assertEqual(ctx["tool_definitions_tokens"], 5_600)
        self.assertEqual(
            ctx["usage_categories"],
            [
                {"label": "Skills", "tokens": 2_400, "detail": "21 skills"},
                {"label": "MCP servers", "tokens": 320, "detail": "4 servers"},
            ],
        )

    def test_emit_uses_context_usage_event(self):
        calls = []
        emit_context_snapshot("agent-1", 7, sample_info(), lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)
        agent_id, turn_id, event_type, summary, payload = calls[0]
        self.assertEqual((agent_id, turn_id, event_type), ("agent-1", 7, "context_usage"))
        self.assertIn("36.7k", summary)
        self.assertEqual(payload["context"]["auto_compact_threshold_percent"], 85)


class AcpContextTest(unittest.TestCase):
    def test_unwrap_accepts_extension_envelope_and_raw_response(self):
        wrapped = {"result": {"sessionId": "s", "context": {"total": 10}}, "error": None}
        self.assertEqual(unwrap_session_info(wrapped)["sessionId"], "s")
        raw = {"sessionId": "s2", "context": {"total": 10}}
        self.assertEqual(unwrap_session_info(raw)["sessionId"], "s2")

    def test_pick_auth_method_prefers_agent_default(self):
        init = {
            "_meta": {"defaultAuthMethodId": "cached_token"},
            "authMethods": [{"id": "xai.api_key"}],
        }
        self.assertEqual(_pick_auth_method(init), "cached_token")
        self.assertEqual(_pick_auth_method({"authMethods": [{"id": "xai.api_key"}]}), "xai.api_key")

    def test_context_summary_formats_snapshot(self):
        self.assertEqual(
            context_summary({"context": {"used": 200, "total": 1000, "usagePct": 20}}),
            "Context 200/1000 (20%)",
        )

    def test_probe_rejects_missing_session_without_spawning(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(AcpProbeError, "missing Grok session id"):
                probe_session_context("", Path(folder))


class LiveStreamingTest(unittest.TestCase):
    def _load_module(self):
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
        sys.modules["daemon"] = fake
        sys.modules.pop("live_streaming", None)
        module = importlib.import_module("live_streaming")
        return module, fake, calls

    def tearDown(self):
        sys.modules.pop("live_streaming", None)
        sys.modules.pop("daemon", None)

    def test_live_chunks_supersede_later_coalesced_stdout(self):
        module, fake, calls = self._load_module()
        module.install_live_streaming()
        self.assertNotIn("agent_message_chunk", fake.SESSION_SKIP_TYPES)
        fake.add_event("a", 1, "agent_message_chunk", "hello", {"content": "hello"})
        fake.add_event("a", 1, "text", "hello", {"coalesced": True, "data": "hello"})
        self.assertEqual([call[2] for call in calls], ["text"])
        self.assertTrue(calls[0][4]["observer_live_chunk"])

    def test_live_chunk_closes_stdout_before_monitor_race(self):
        module, fake, calls = self._load_module()
        module.install_live_streaming()
        fake.add_event("a", 2, "text", "coalesced", {"coalesced": True, "data": "coalesced"})
        self.assertEqual(calls, [])
        fake.add_event("a", 2, "agent_message_chunk", "live", {"content": "live"})
        module._flush_turn_fallbacks("a", 2)
        self.assertEqual([call[3] for call in calls], ["live"])

    def test_coalesced_stdout_is_preserved_when_live_source_absent(self):
        module, fake, calls = self._load_module()
        module.install_live_streaming()
        fake.add_event("a", 3, "thought", "thinking", {"coalesced": True, "data": "thinking"})
        self.assertEqual(calls, [])
        # interrupted is terminal for cleanup/fallback but does not schedule ACP.
        fake.add_event("a", 3, "interrupted", "interrupted", {})
        self.assertEqual([call[2] for call in calls], ["thought", "interrupted"])
        self.assertEqual(calls[0][3], "thinking")


if __name__ == "__main__":
    unittest.main()
