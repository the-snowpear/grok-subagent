"""Live-streaming launcher for the Grok observer daemon.

The base daemon deliberately coalesces stdout text/thought chunks before SQLite
persistence.  Recent Grok sessions also write fine-grained
agent_message_chunk / agent_thought_chunk updates to the session log.  This
launcher promotes those already-available chunks into the normal text/thought
event stream and suppresses the later coalesced duplicate.

Keeping this as a launcher avoids forking daemon.py and makes the streaming
behavior easy to remove once upstream exposes a configurable flush interval.
"""

from __future__ import annotations

import threading

import daemon as observer


LIVE_SOURCE_TYPES = {
    "agent_message_chunk": "text",
    "agent_thought_chunk": "thought",
}

_original_add_event = observer.add_event
_live_seen: set[tuple[str, int | None, str]] = set()
_live_seen_lock = threading.Lock()


def _payload_with_live_marker(payload, source_type: str) -> dict:
    if isinstance(payload, dict):
        normalized = dict(payload)
    elif payload is None:
        normalized = {}
    else:
        normalized = {"value": payload}
    normalized["observer_live_chunk"] = True
    normalized["observer_source_type"] = source_type
    return normalized


def live_add_event(
    agent_id: str,
    turn_id: int | None,
    event_type: str,
    summary: str,
    payload=None,
) -> int:
    """Promote session chunks and drop the corresponding buffered duplicate."""
    mapped_type = LIVE_SOURCE_TYPES.get(event_type)
    if mapped_type:
        key = (agent_id, turn_id, mapped_type)
        with _live_seen_lock:
            _live_seen.add(key)
        return _original_add_event(
            agent_id,
            turn_id,
            mapped_type,
            summary,
            _payload_with_live_marker(payload, event_type),
        )

    if event_type in {"text", "thought"} and isinstance(payload, dict) and payload.get("coalesced"):
        key = (agent_id, turn_id, event_type)
        with _live_seen_lock:
            has_live_chunks = key in _live_seen
        if has_live_chunks:
            # The fine-grained session chunks have already been persisted and
            # emitted through SSE. Returning 0 is safe: callers only need the
            # side effect of add_event for this path.
            return 0

    return _original_add_event(agent_id, turn_id, event_type, summary, payload)


def install_live_streaming() -> None:
    # These two session events were previously skipped because stdout emitted a
    # later coalesced copy.  Keep every other skip rule unchanged.
    observer.SESSION_SKIP_TYPES = frozenset(
        event_type
        for event_type in observer.SESSION_SKIP_TYPES
        if event_type not in LIVE_SOURCE_TYPES
    )
    observer.add_event = live_add_event


if __name__ == "__main__":
    install_live_streaming()
    observer.main()
