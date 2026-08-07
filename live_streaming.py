"""Live-streaming + telemetry launcher for the Grok observer daemon.

The base daemon intentionally coalesces headless stdout text/thought chunks
before persisting them. Recent Grok sessions also write fine-grained
``agent_message_chunk`` / ``agent_thought_chunk`` updates to the session log.
This launcher promotes those chunks into the normal observer stream, suppresses
the later coalesced duplicate, and captures Grok's semantic ContextInfo after
an idle terminal turn.

Keeping the behavior in a launcher avoids forking the large daemon module and
lets existing installs keep using the same data schema and viewer server.
"""

from __future__ import annotations

import os
import sys
import threading

import daemon as observer
from context_telemetry import emit_context_snapshot
from grok_acp_context import AcpProbeError, probe_session_context


LIVE_SOURCE_TYPES = {
    "agent_message_chunk": "text",
    "agent_thought_chunk": "thought",
}
_CONTEXT_TERMINAL_TYPES = frozenset({"completed", "failed"})
_CONTEXT_TERMINAL_AGENT_STATES = frozenset({"completed", "failed"})

_original_add_event = observer.add_event
_live_seen: set[tuple[str, int | None, str]] = set()
_live_seen_lock = threading.Lock()
_probe_inflight: set[tuple[str, int]] = set()
_probe_lock = threading.Lock()


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


def _context_telemetry_enabled() -> bool:
    raw = os.environ.get("GROK_OBSERVER_CONTEXT_TELEMETRY", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _context_probe_target(agent_id: str, turn_id: int) -> tuple[str, str] | None:
    """Return ``(cwd, session_id)`` only while this terminal turn is still idle.

    A replacement/follow-up turn can start immediately after the terminal event.
    Context probing must never compete with an active writer for the same Grok
    session, so both the aggregate agent state and current turn are rechecked.
    """
    try:
        with observer.connect() as db:
            row = db.execute(
                "SELECT cwd,grok_session_id,status,current_turn FROM agents WHERE id=?",
                (agent_id,),
            ).fetchone()
            if not row:
                return None
            if str(row["status"] or "") not in _CONTEXT_TERMINAL_AGENT_STATES:
                return None
            if row["current_turn"] is None or int(row["current_turn"]) != int(turn_id):
                return None
            pending = db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE agent_id=? AND status IN ('queued','running')",
                (agent_id,),
            ).fetchone()
            if pending and int(pending["c"] or 0) > 0:
                return None
            return str(row["cwd"]), str(row["grok_session_id"])
    except Exception:
        return None


def _probe_context_worker(agent_id: str, turn_id: int) -> None:
    key = (agent_id, int(turn_id))
    try:
        target = _context_probe_target(agent_id, turn_id)
        if not target:
            return
        cwd, session_id = target
        child_env, _proxy_source = observer.system_proxy_environment(os.environ.copy())
        info = probe_session_context(session_id, cwd, env=child_env, timeout=8.0)

        # The ACP request can take a few seconds. Drop a stale snapshot if a new
        # observer turn started while it was running.
        if _context_probe_target(agent_id, turn_id) is None:
            return
        emit_context_snapshot(agent_id, turn_id, info, observer.add_event)
    except (AcpProbeError, OSError, ValueError, RuntimeError) as exc:
        if os.environ.get("GROK_OBSERVER_CONTEXT_DEBUG", "").strip():
            print(f"[grok-observer] context probe skipped: {exc}", file=sys.stderr)
    except Exception as exc:  # observability must never fail a delegated turn
        if os.environ.get("GROK_OBSERVER_CONTEXT_DEBUG", "").strip():
            print(f"[grok-observer] context probe error: {exc!r}", file=sys.stderr)
    finally:
        with _probe_lock:
            _probe_inflight.discard(key)


def _schedule_context_probe(agent_id: str, turn_id: int | None) -> None:
    if turn_id is None or not _context_telemetry_enabled():
        return
    # Fake-Grok tests intentionally do not provide an ACP binary/session store.
    if os.environ.get("GROK_OBSERVER_FAKE_GROK"):
        return
    key = (agent_id, int(turn_id))
    with _probe_lock:
        if key in _probe_inflight:
            return
        _probe_inflight.add(key)
    thread = threading.Thread(
        target=_probe_context_worker,
        args=(agent_id, int(turn_id)),
        name=f"context-{agent_id[:8]}-{turn_id}",
        daemon=True,
    )
    thread.start()


def live_add_event(
    agent_id: str,
    turn_id: int | None,
    event_type: str,
    summary: str,
    payload=None,
) -> int:
    """Promote session chunks, suppress buffered duplicates, schedule ContextInfo."""
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
            # Fine-grained session chunks have already been stored and emitted.
            # Callers use add_event for the side effect only on this path.
            return 0

    revision = _original_add_event(agent_id, turn_id, event_type, summary, payload)
    if event_type in _CONTEXT_TERMINAL_TYPES:
        _schedule_context_probe(agent_id, turn_id)
    return revision


def install_live_streaming() -> None:
    # These two session events were previously skipped because stdout emitted a
    # later coalesced copy. Keep every other skip rule unchanged.
    observer.SESSION_SKIP_TYPES = frozenset(
        event_type
        for event_type in observer.SESSION_SKIP_TYPES
        if event_type not in LIVE_SOURCE_TYPES
    )
    observer.add_event = live_add_event


if __name__ == "__main__":
    install_live_streaming()
    observer.main()
