"""Context telemetry storage helpers.

Stores ACP-derived ContextInfo as an observer event payload.  The viewer can
consume this without coupling itself to ACP transport details.
"""

from __future__ import annotations

import json
from typing import Any, Callable



def normalize_context_info(info: dict[str, Any]) -> dict[str, Any]:
    context = info.get("context") if isinstance(info.get("context"), dict) else {}
    categories = context.get("usageCategories")
    if not isinstance(categories, list):
        categories = []
    return {
        "session_id": info.get("sessionId"),
        "cwd": info.get("cwd"),
        "agent_name": info.get("agentName"),
        "model": info.get("model"),
        "context": {
            "used": int(context.get("used") or 0),
            "total": int(context.get("total") or 0),
            "system_prompt_tokens": int(context.get("systemPromptTokens") or 0),
            "tool_definitions_count": int(context.get("toolDefinitionsCount") or 0),
            "tool_definitions_tokens": int(context.get("toolDefinitionsTokens") or 0),
            "message_count": int(context.get("messageCount") or 0),
            "message_tokens": int(context.get("messageTokens") or 0),
            "free_tokens": int(context.get("freeTokens") or 0),
            "usage_pct": int(context.get("usagePct") or 0),
            "compaction_count": int(context.get("compactionCount") or 0),
            "turn_count": int(context.get("turnCount") or 0),
            "tool_call_count": int(context.get("toolCallCount") or 0),
            "auto_compact_threshold_percent": int(
                context.get("autoCompactThresholdPercent") or 85
            ),
            "usage_categories": categories,
        },
    }


def emit_context_snapshot(
    agent_id: str,
    turn_id: int | None,
    info: dict[str, Any],
    add_event: Callable[..., Any],
) -> None:
    """Persist a context snapshot as an observer event."""
    payload = normalize_context_info(info)
    add_event(
        agent_id,
        turn_id,
        "context_usage",
        json.dumps(payload.get("context", {}), ensure_ascii=False),
        payload,
    )
