"""Live-streaming + semantic telemetry launcher for the Grok observer daemon.

The base daemon deliberately coalesces headless stdout text/thought chunks before
persisting them. Recent Grok sessions also write fine-grained
``agent_message_chunk`` / ``agent_thought_chunk`` updates to the session log.
This launcher promotes those session-log chunks into the normal observer stream
and keeps the old coalesced stdout path as a race-safe compatibility fallback.

After an idle terminal turn, a short-lived ACP process loads the persisted Grok
session and reads ``x.ai/session/info`` so the viewer can show Grok's own semantic
ContextInfo breakdown without changing how delegated work itself is executed.
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
_TURN_TERMINAL_TYPES = frozenset({"completed", "failed", "interrupted", "cancelled"})
_CONTEXT_TERMINAL_TYPES = frozenset({"completed", "failed"})
_CONTEXT_TERMINAL_AGENT_STATES = frozenset({"completed", "failed"})

_original_add_event = observer.add_event
_stream_lock = threading.Lock()
_live_seen: set[tuple[str, int | None, str]] = set()
# Preserve coalesced stdout ordering within a turn. A later live session chunk
# deletes only fallback rows of the same mapped type, closing the race where
# stdout can flush before the session monitor observes the fine-grained source.
_coalesced_fallbacks: dict[
    tuple[str, int | None],
    list[tuple[str, str, object]],
] = {}
_probe_inflight: set[tuple[str, int]] = set()
_probe_lock = threading.Lock()
_installed = False


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


def _turn_key(agent_id: str, turn_id: int | None) -> tuple[str, int | None]:
    return agent_id, turn_id


def _flush_turn_fallbacks(agent_id: str, turn_id: int | None) -> None:
    """Emit only coalesced stdout rows not superseded by live session chunks."""
    turn_key = _turn_key(agent_id, turn_id)
    with _stream_lock:
        rows = _coalesced_fallbacks.pop(turn_key, [])
        for event_type in ("text", "thought"):
            _live_seen.discard((agent_id, turn_id, event_type))
    for event_type, summary, payload in rows:
        _original_add_event(agent_id, turn_id, event_type, summary, payload)


def _context_telemetry_enabled() -> bool:
    raw = os.environ.get("GROK_OBSERVER_CONTEXT_TELEMETRY", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _context_probe_timeout() -> float:
    raw = os.environ.get("GROK_OBSERVER_CONTEXT_PROBE_TIMEOUT", "8")
    try:
        return max(2.0, min(20.0, float(raw)))
    except ValueError:
        return 8.0


def _context_probe_target(agent_id: str, turn_id: int) -> tuple[str, str] | None:
    """Return ``(cwd, session_id)`` only while the terminal turn is still idle.

    A queued/replacement turn can start immediately. The disposable ACP
    ``session/load`` must not compete with a new headless writer for the same
    persisted session, so both aggregate agent state and active turns are
    rechecked before and after probing.
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
            session_id = str(row["grok_session_id"] or "").strip()
            cwd = str(row["cwd"] or "").strip()
            if not cwd or not session_id:
                return None
            return cwd, session_id
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
        info = probe_session_context(
            session_id,
            cwd,
            env=child_env,
            timeout=_context_probe_timeout(),
        )
        # Drop a stale snapshot if a follow-up turn started while ACP was loading.
        if _context_probe_target(agent_id, turn_id) is None:
            return
        emit_context_snapshot(agent_id, turn_id, info, _original_add_event)
    except (AcpProbeError, OSError, ValueError, RuntimeError) as exc:
        if os.environ.get("GROK_OBSERVER_CONTEXT_DEBUG", "").strip():
            print(f"[grok-observer] context probe skipped: {exc}", file=sys.stderr)
    except Exception as exc:  # telemetry must never fail delegated work
        if os.environ.get("GROK_OBSERVER_CONTEXT_DEBUG", "").strip():
            print(f"[grok-observer] context probe error: {exc!r}", file=sys.stderr)
    finally:
        with _probe_lock:
            _probe_inflight.discard(key)


def _schedule_context_probe(agent_id: str, turn_id: int | None) -> None:
    if turn_id is None or not _context_telemetry_enabled():
        return
    # Fake-Grok fixtures intentionally do not expose an ACP session store.
    if os.environ.get("GROK_OBSERVER_FAKE_GROK"):
        return
    key = (agent_id, int(turn_id))
    with _probe_lock:
        if key in _probe_inflight:
            return
        _probe_inflight.add(key)
    threading.Thread(
        target=_probe_context_worker,
        args=(agent_id, int(turn_id)),
        name=f"context-{agent_id[:8]}-{turn_id}",
        daemon=True,
    ).start()


def live_add_event(
    agent_id: str,
    turn_id: int | None,
    event_type: str,
    summary: str,
    payload=None,
) -> int:
    """Promote live chunks, de-duplicate stdout fallback, and schedule ContextInfo."""
    mapped_type = LIVE_SOURCE_TYPES.get(event_type)
    if mapped_type:
        live_key = (agent_id, turn_id, mapped_type)
        turn_key = _turn_key(agent_id, turn_id)
        with _stream_lock:
            _live_seen.add(live_key)
            buffered = _coalesced_fallbacks.get(turn_key)
            if buffered:
                kept = [row for row in buffered if row[0] != mapped_type]
                if kept:
                    _coalesced_fallbacks[turn_key] = kept
                else:
                    _coalesced_fallbacks.pop(turn_key, None)
        return _original_add_event(
            agent_id,
            turn_id,
            mapped_type,
            summary,
            _payload_with_live_marker(payload, event_type),
        )

    if event_type in {"text", "thought"} and isinstance(payload, dict) and payload.get("coalesced"):
        live_key = (agent_id, turn_id, event_type)
        turn_key = _turn_key(agent_id, turn_id)
        with _stream_lock:
            if live_key in _live_seen:
                return 0
            _coalesced_fallbacks.setdefault(turn_key, []).append((event_type, summary, payload))
        # Hold the legacy copy until the daemon's terminal status event. The
        # session monitor performs its final drain before that event is emitted,
        # so any authoritative live chunks get a chance to supersede this row.
        return 0

    if event_type in _TURN_TERMINAL_TYPES:
        # Flush compatibility output before the terminal marker so timeline order
        # remains text/thought -> completed/failed. Also clears per-turn state.
        _flush_turn_fallbacks(agent_id, turn_id)

    revision = _original_add_event(agent_id, turn_id, event_type, summary, payload)
    if event_type in _CONTEXT_TERMINAL_TYPES:
        _schedule_context_probe(agent_id, turn_id)
    return revision


def install_live_streaming() -> None:
    global _installed
    if _installed:
        return
    # The base monitor skips these because headless stdout used to be the sole
    # text/thought source. Keep every other skip rule unchanged.
    observer.SESSION_SKIP_TYPES = frozenset(
        event_type
        for event_type in observer.SESSION_SKIP_TYPES
        if event_type not in LIVE_SOURCE_TYPES
    )
    observer.add_event = live_add_event
    _installed = True


if __name__ == "__main__":
    install_live_streaming()
    observer.main()
