"""Model-facing tool boundary regression tests (P1-A).

These tests start from the REAL Codex-facing MCP schema — ``server.TOOLS``,
the exact list served by ``tools/list`` — and prove that ``role`` and
``reasoning_effort`` are visible to the model on ``create_agent`` /
``create_agents`` and survive the real dispatch path (``tools/call`` ->
``server.call_tool`` -> ``daemon.action``) verbatim, landing in
``agents.role`` and ``agents.reasoning_effort``.

No fake schema is built in the test; no source-string inspection is used;
calling ``daemon.action`` alone is never presented as an MCP proof. The
schema assertions read the canonical registry, the dispatch assertions run
the real tool call helper with only the socket transport mocked, and the
persistence assertions run the exact same args through the daemon runtime.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import daemon
import server as mcp_server
from tests.test_orchestrate_v2 import OrchestrateV2Mixin


def _tool_schema(name: str) -> dict:
    return next(tool for tool in mcp_server.TOOLS if tool["name"] == name)


class ModelBoundarySchemaTests(unittest.TestCase):
    """A1-A2: the exported tools/list schema advertises the fields."""

    def test_create_agent_schema_exposes_role_and_reasoning_effort(self):
        """A1: create_agent lets the model pass role and reasoning_effort."""
        schema = _tool_schema("create_agent")
        properties = schema["inputSchema"]["properties"]
        self.assertIn("role", properties, "role must be model-visible on create_agent")
        role = properties["role"]
        self.assertEqual(role["type"], "string")
        self.assertEqual(set(role["enum"]), {"explore", "implement", "review"})
        self.assertIn(
            "reasoning_effort", properties,
            "reasoning_effort must be model-visible on create_agent",
        )
        self.assertEqual(properties["reasoning_effort"]["type"], "string")
        # additionalProperties=False means an absent field would be rejected by
        # strict MCP clients; the field must live in the schema itself.
        self.assertFalse(schema["inputSchema"].get("additionalProperties", False))

    def test_create_agents_schema_exposes_batch_and_per_item_fields(self):
        """A2: create_agents carries batch defaults plus per-item overrides."""
        schema = _tool_schema("create_agents")
        top = schema["inputSchema"]["properties"]
        self.assertIn("role", top, "batch role default must be model-visible")
        self.assertEqual(set(top["role"]["enum"]), {"explore", "implement", "review"})
        self.assertIn("reasoning_effort", top, "batch effort default must be model-visible")
        self.assertEqual(top["reasoning_effort"]["type"], "string")
        items = top["agents"]["items"]["properties"]
        self.assertIn("role", items, "per-item role must be model-visible")
        self.assertIn("reasoning_effort", items, "per-item effort must be model-visible")
        self.assertFalse(schema["inputSchema"].get("additionalProperties", False))


class ModelBoundaryDispatchTests(OrchestrateV2Mixin, unittest.TestCase):
    """A3: the real dispatch path keeps the fields and persists them."""

    def _capture_dispatch(self, name: str, args: dict) -> dict:
        """Run the real server.call_tool with only the socket transport stubbed."""
        captured: list[dict] = []

        def _fake_request(payload: dict, timeout: float = 65) -> dict:
            captured.append(payload)
            return {
                "ok": True,
                "data": {
                    "agent_id": "dispatch-probe",
                    "status": "queued",
                    "viewer_url": "http://127.0.0.1:0/#/agents/dispatch-probe",
                },
            }

        with mock.patch.object(mcp_server, "_state", return_value={"control_port": 1}), (
            mock.patch.object(mcp_server, "_request", side_effect=_fake_request)
        ):
            mcp_server.call_tool(name, args)
        self.assertEqual(len(captured), 2, "ping probe + one tool call must be dispatched")
        return captured[1]

    def test_create_agent_dispatch_keeps_role_and_effort_verbatim(self):
        """A3a: tools/call -> call_tool -> daemon args must not strip/rename."""
        payload = self._capture_dispatch(
            "create_agent",
            {
                "agent_name": "boundary",
                "prompt": "do work",
                "cwd": str(self.root),
                "role": "explore",
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(payload["action"], "create_agent")
        self.assertEqual(payload["args"]["role"], "explore")
        self.assertEqual(payload["args"]["reasoning_effort"], "high")

    def test_create_agents_dispatch_keeps_batch_and_per_item_fields(self):
        """A3b: create_agents batch + per-item fields survive dispatch."""
        payload = self._capture_dispatch(
            "create_agents",
            {
                "agents": [
                    {"agent_name": "a1", "prompt": "p1", "cwd": str(self.root)},
                    {
                        "agent_name": "a2",
                        "prompt": "p2",
                        "cwd": str(self.root),
                        "role": "implement",
                        "reasoning_effort": "high",
                    },
                ],
                "role": "explore",
                "reasoning_effort": "low",
            },
        )
        self.assertEqual(payload["action"], "create_agents")
        self.assertEqual(payload["args"]["role"], "explore")
        self.assertEqual(payload["args"]["reasoning_effort"], "low")
        self.assertEqual(payload["args"]["agents"][0].get("role"), None)
        self.assertEqual(payload["args"]["agents"][1]["role"], "implement")
        self.assertEqual(payload["args"]["agents"][1]["reasoning_effort"], "high")

    def test_dispatched_args_persist_to_agents_row(self):
        """A3c: the same args through the real daemon land in agents.role/effort."""
        thread_id = "t-model-boundary-persist"
        created = daemon.action(
            "create_agent",
            {
                "agent_name": "boundary",
                "prompt": "do work",
                "cwd": str(self.root),
                "role": "review",
                "reasoning_effort": "max",
            },
            {"codex_thread_id": thread_id, "codex_origin": "test"},
        )
        aid = created["agent_id"]
        row = self._agent_row(aid)
        self.assertEqual(row["role"], "review")
        self.assertEqual(row["reasoning_effort"], "max")
        status = daemon.action("status", {"agent_id": aid}, {"codex_thread_id": thread_id})
        self.assertEqual(status["role"], "review")
        self.assertEqual(status["reasoning_effort"], "max")


if __name__ == "__main__":
    unittest.main()
