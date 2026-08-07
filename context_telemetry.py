"""Normalize ACP-derived Grok ContextInfo into observer events."""

from __future__ import annotations

from typing import Any, Callable


def _non_negative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _usage_categories(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        detail = item.get("detail")
        rows.append(
            {
                "label": label,
                "tokens": _non_negative_int(item.get("tokens")),
                "detail": str(detail).strip() if detail is not None else None,
            }
        )
    return rows


def normalize_context_info(info: dict[str, Any]) -> dict[str, Any]:
    context = info.get("context") if isinstance(info.get("context"), dict) else {}
    total = _non_negative_int(context.get("total"))
    raw_used = _non_negative_int(context.get("used"))
    used = min(raw_used, total) if total else raw_used
    usage_pct = _non_negative_int(context.get("usagePct"))
    if total and not usage_pct and used:
        usage_pct = min(100, round((used / total) * 100))

    return {
        "session_id": info.get("sessionId"),
        "cwd": info.get("cwd"),
        "agent_name": info.get("agentName"),
        "model": info.get("model"),
        "model_display_name": info.get("modelDisplayName"),
        "resolved_model_id": info.get("resolvedModelId"),
        "context": {
            "used": used,
            "total": total,
            "system_prompt_tokens": _non_negative_int(context.get("systemPromptTokens")),
            "tool_definitions_count": _non_negative_int(context.get("toolDefinitionsCount")),
            "tool_definitions_tokens": _non_negative_int(context.get("toolDefinitionsTokens")),
            "compaction_count": _non_negative_int(context.get("compactionCount")),
            "turn_count": _non_negative_int(context.get("turnCount")),
            "tool_call_count": _non_negative_int(context.get("toolCallCount")),
            "message_count": _non_negative_int(context.get("messageCount")),
            "message_tokens": _non_negative_int(context.get("messageTokens")),
            "free_tokens": _non_negative_int(context.get("freeTokens")),
            "usage_pct": min(100, usage_pct),
            "auto_compact_threshold_percent": min(
                100,
                _non_negative_int(context.get("autoCompactThresholdPercent"), 85),
            ),
            # Grok's own /context UI treats Skills/MCP rows as informational:
            # they overlap Messages and must not be added to `used` again.
            "usage_categories": _usage_categories(context.get("usageCategories")),
        },
    }


def _compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M".rstrip("0").rstrip(".")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return str(value)


def emit_context_snapshot(
    agent_id: str,
    turn_id: int | None,
    info: dict[str, Any],
    add_event: Callable[..., Any],
) -> None:
    """Persist the newest semantic context snapshot as ``context_usage``."""
    payload = normalize_context_info(info)
    context = payload["context"]
    total = int(context.get("total") or 0)
    used = int(context.get("used") or 0)
    pct = int(context.get("usage_pct") or 0)
    summary = (
        f"Context {_compact(used)} / {_compact(total)} ({pct}%)"
        if total
        else "Context telemetry"
    )
    add_event(agent_id, turn_id, "context_usage", summary, payload)
