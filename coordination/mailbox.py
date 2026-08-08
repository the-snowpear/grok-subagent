"""Durable SQLite mailbox.

Critical invariants:
- SQLite is authoritative.
- send commits before notify.
- inbox drain is atomic.
- wait has no send-between-query-and-sleep lost wakeup.
- messages claimed by an automatic delivery turn are hidden from inbox/wait;
  a pre-spawn failure releases the claim and makes them visible again.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager as ContextManager
from dataclasses import dataclass
from sqlite3 import Connection

from .types import MAX_INBOX_BYTES, MAX_INBOX_MESSAGES, MAX_MESSAGE_BYTES, Message

ConnectFactory = Callable[..., ContextManager[Connection]]
NowFactory = Callable[[], str]


@dataclass
class _WaitState:
    condition: threading.Condition
    revision: int = 0


class Mailbox:
    def __init__(
        self,
        connect_factory: ConnectFactory,
        now_factory: NowFactory,
        on_message_committed: Callable[[Message], None] | None = None,
    ):
        self._connect = connect_factory
        self._now = now_factory
        self._on_message_committed = on_message_committed
        self._states_lock = threading.Lock()
        self._states: dict[str, _WaitState] = {}

    def _wait_state(self, peer_id: str) -> _WaitState:
        with self._states_lock:
            state = self._states.get(peer_id)
            if state is None:
                state = _WaitState(condition=threading.Condition())
                self._states[peer_id] = state
            return state

    def notify(self, peer_id: str) -> None:
        state = self._wait_state(peer_id)
        with state.condition:
            state.revision += 1
            state.condition.notify_all()

    @staticmethod
    def validate_body(body: object) -> str:
        if not isinstance(body, str):
            raise ValueError("message must be a string")
        if not body.strip():
            raise ValueError("message must not be empty")
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message exceeds {MAX_MESSAGE_BYTES} bytes")
        return body

    @staticmethod
    def _row_to_message(row) -> Message:
        return Message(
            id=str(row["id"]),
            thread_id=str(row["thread_id"]),
            from_peer=str(row["from_peer"]),
            to_peer=str(row["to_peer"]),
            kind=str(row["kind"]),
            body=str(row["body"]),
            reply_to=row["reply_to"],
            delivery_mode=str(row["delivery_mode"]),
            state=str(row["state"]),
            target_turn_id=row["target_turn_id"],
            error=row["error"],
            created_at=str(row["created_at"]),
            delivered_at=row["delivered_at"],
            consumed_at=row["consumed_at"],
        )

    def send(
        self,
        *,
        thread_id: str,
        from_peer: str,
        to_peer: str,
        body: str,
        reply_to: str | None = None,
    ) -> Message:
        body = self.validate_body(body)
        if from_peer == to_peer:
            raise ValueError("cannot send a message to self")

        message_id = str(uuid.uuid4())
        created_at = self._now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO agent_messages(
                    id, thread_id, from_peer, to_peer,
                    kind, body, reply_to, delivery_mode,
                    state, target_turn_id, error,
                    created_at, delivered_at, consumed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    thread_id,
                    from_peer,
                    to_peer,
                    "message",
                    body,
                    reply_to,
                    "queue",
                    "pending",
                    None,
                    None,
                    created_at,
                    None,
                    None,
                ),
            )

        self.notify(to_peer)
        message = Message(
            id=message_id,
            thread_id=thread_id,
            from_peer=from_peer,
            to_peer=to_peer,
            kind="message",
            body=body,
            reply_to=reply_to,
            delivery_mode="queue",
            state="pending",
            target_turn_id=None,
            error=None,
            created_at=created_at,
            delivered_at=None,
            consumed_at=None,
        )

        # The row is already durable. Scheduling is a best-effort wake path;
        # reporting the send as failed after commit encourages client retries and
        # duplicates. Startup/terminal sweeps can schedule it later.
        if self._on_message_committed is not None:
            try:
                self._on_message_committed(message)
            except Exception:
                pass
        return message

    def mark_delivered_for_turn(self, *, turn_id: int) -> int:
        """Mark auto-injected messages delivered *and consumed*.

        A worker that received the message in its follow-up prompt must not see
        the same message again through inbox/wait.
        """
        stamp = self._now()
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE agent_messages
                SET state='delivered', delivered_at=?, consumed_at=COALESCE(consumed_at, ?)
                WHERE target_turn_id=? AND state='pending'
                """,
                (stamp, stamp, turn_id),
            )
            return cursor.rowcount

    def release_scheduled_for_turn(self, *, turn_id: int) -> int:
        """Release claims for a delivery turn that failed before child spawn."""
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE agent_messages
                SET target_turn_id=NULL
                WHERE target_turn_id=? AND state='pending' AND consumed_at IS NULL
                """,
                (turn_id,),
            )
            return cursor.rowcount

    def reconcile_turn_delivery(self, *, turn_id: int, started: bool) -> int:
        """Idempotent crash-consistency primitive for one turn's delivery claim.

        started=True converges pending claims to delivered+consumed: the child
        received the turn prompt, so the message must never be injected twice.
        started=False releases claims: the child never spawned, so the messages
        become visible again. Safe to call repeatedly; rows already in the
        target state are untouched. Bounded busy retry absorbs transient
        locked/busy contention; other errors propagate immediately.
        """
        stamp = self._now()
        if started:
            sql = (
                "UPDATE agent_messages "
                "SET state='delivered', delivered_at=COALESCE(delivered_at, ?), "
                "consumed_at=COALESCE(consumed_at, ?) "
                "WHERE target_turn_id=? AND state='pending'"
            )
            params: tuple = (stamp, stamp, turn_id)
        else:
            sql = (
                "UPDATE agent_messages "
                "SET target_turn_id=NULL "
                "WHERE target_turn_id=? AND state='pending' AND consumed_at IS NULL"
            )
            params = (turn_id,)
        retries = (0.02, 0.04, 0.06)
        attempt = 0
        while True:
            try:
                with self._connect() as db:
                    cursor = db.execute(sql, params)
                    return cursor.rowcount
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if attempt >= len(retries):
                    raise
                time.sleep(retries[attempt])
                attempt += 1

    def _select_unconsumed(
        self,
        *,
        peer_id: str,
        from_peer: str | None = None,
        limit: int = MAX_INBOX_MESSAGES,
    ) -> list[Message]:
        where = [
            "to_peer=?",
            "state='pending'",
            "consumed_at IS NULL",
            "target_turn_id IS NULL",
        ]
        params: list[object] = [peer_id]
        if from_peer is not None:
            where.append("from_peer=?")
            params.append(from_peer)
        params.append(max(1, min(limit, MAX_INBOX_MESSAGES)))
        sql = (
            "SELECT * FROM agent_messages "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at ASC, id ASC LIMIT ?"
        )
        with self._connect() as db:
            rows = db.execute(sql, tuple(params)).fetchall()

        result: list[Message] = []
        used = 0
        for row in rows:
            msg = self._row_to_message(row)
            size = len(msg.body.encode("utf-8"))
            if result and used + size > MAX_INBOX_BYTES:
                break
            # Never silently lose a single message because it alone is larger
            # than the aggregate inbox byte cap.
            result.append(msg)
            used += size
        return result

    def inbox(self, *, peer_id: str, peek: bool = False) -> list[Message]:
        if peek:
            return self._select_unconsumed(peer_id=peer_id)

        stamp = self._now()
        with self._connect(immediate=True) as db:
            rows = db.execute(
                """
                SELECT * FROM agent_messages
                WHERE to_peer=?
                  AND state='pending'
                  AND consumed_at IS NULL
                  AND target_turn_id IS NULL
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (peer_id, MAX_INBOX_MESSAGES),
            ).fetchall()

            selected = []
            used = 0
            for row in rows:
                msg = self._row_to_message(row)
                size = len(msg.body.encode("utf-8"))
                if selected and used + size > MAX_INBOX_BYTES:
                    break
                selected.append(msg)
                used += size

            for msg in selected:
                db.execute(
                    """
                    UPDATE agent_messages
                    SET state='consumed', consumed_at=?
                    WHERE id=?
                      AND state='pending'
                      AND consumed_at IS NULL
                      AND target_turn_id IS NULL
                    """,
                    (stamp, msg.id),
                )
        return selected

    def wait(
        self,
        *,
        peer_id: str,
        from_peer: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> Message | None:
        timeout_seconds = max(0.0, float(timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        state = self._wait_state(peer_id)

        while True:
            messages = self._select_unconsumed(
                peer_id=peer_id,
                from_peer=from_peer,
                limit=1,
            )
            if messages:
                return messages[0]

            with state.condition:
                before = state.revision

            # Second DB check closes query-empty -> commit+notify -> sleep.
            messages = self._select_unconsumed(
                peer_id=peer_id,
                from_peer=from_peer,
                limit=1,
            )
            if messages:
                return messages[0]

            with state.condition:
                if state.revision != before:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                state.condition.wait(timeout=remaining)

    def peek_one(
        self,
        *,
        peer_id: str,
        from_peer: str | None = None,
    ) -> Message | None:
        messages = self._select_unconsumed(
            peer_id=peer_id,
            from_peer=from_peer,
            limit=1,
        )
        return messages[0] if messages else None

    def wait_surface(self, peer_id: str) -> tuple[threading.Condition, int]:
        state = self._wait_state(peer_id)
        with state.condition:
            return state.condition, state.revision

    def revision(self, peer_id: str) -> int:
        state = self._wait_state(peer_id)
        with state.condition:
            return state.revision
