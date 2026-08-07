"""Durable SQLite mailbox.

Critical invariants:
- SQLite is authoritative.
- send commits before notify.
- inbox drain is atomic.
- wait has no send-between-query-and-sleep lost wakeup.

The connect factory must accept a keyword-only `immediate: bool = False`.
With immediate=True it yields a BEGIN IMMEDIATE write transaction (PRAGMAs
run first) that commits on clean exit and rolls back on exception; with
False it behaves like daemon.connect().
"""

from __future__ import annotations

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

        # IMPORTANT: commit before notify.
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO agent_messages(
                    id, thread_id, from_peer, to_peer,
                    kind, body, reply_to, delivery_mode,
                    state, target_turn_id, error,
                    created_at, delivered_at, consumed_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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

        # Post-commit hook (outside any DB transaction): lets the daemon
        # schedule delivery without racing the insert.
        if self._on_message_committed is not None:
            self._on_message_committed(message)

        return message

    def pending_for_delivery(
        self,
        *,
        to_peer: str,
        limit: int = MAX_INBOX_MESSAGES,
    ) -> list[Message]:
        """Oldest-first pending messages not yet claimed by a delivery turn."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT *
                FROM agent_messages
                WHERE to_peer=? AND state='pending' AND target_turn_id IS NULL
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (to_peer, max(1, min(limit, MAX_INBOX_MESSAGES))),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def schedule_messages(
        self,
        *,
        thread_id: str,
        message_ids: list[str],
        turn_id: int,
    ) -> int:
        """Claim pending messages for a delivery turn.

        The conditional UPDATE means concurrent schedulers cannot double-claim:
        only messages still pending with no target turn are assigned, and the
        rowcount reflects exactly the claimed set.
        """
        if not message_ids:
            return 0
        placeholders = ",".join("?" for _ in message_ids)
        with self._connect(immediate=True) as db:
            cursor = db.execute(
                f"""
                UPDATE agent_messages
                SET target_turn_id=?
                WHERE id IN ({placeholders})
                  AND state='pending'
                  AND target_turn_id IS NULL
                """,
                (turn_id, *message_ids),
            )
            return cursor.rowcount

    def mark_delivered_for_turn(self, *, turn_id: int) -> int:
        """Mark all pending messages linked to a delivery turn as delivered."""
        delivered_at = self._now()
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE agent_messages
                SET state='delivered', delivered_at=?
                WHERE target_turn_id=? AND state='pending'
                """,
                (delivered_at, turn_id),
            )
            return cursor.rowcount

    def _select_unconsumed(
        self,
        *,
        peer_id: str,
        from_peer: str | None = None,
        limit: int = MAX_INBOX_MESSAGES,
    ) -> list[Message]:
        where = ["to_peer=?", "consumed_at IS NULL"]
        params: list[object] = [peer_id]
        if from_peer is not None:
            where.append("from_peer=?")
            params.append(from_peer)

        sql = (
            "SELECT * FROM agent_messages "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at ASC, id ASC LIMIT ?"
        )
        params.append(max(1, min(limit, MAX_INBOX_MESSAGES)))

        with self._connect() as db:
            rows = db.execute(sql, tuple(params)).fetchall()

        result: list[Message] = []
        used = 0
        for row in rows:
            msg = self._row_to_message(row)
            size = len(msg.body.encode("utf-8"))
            # Oldest-first within the byte cap; a single message (even one
            # larger than the cap) is always returned alone, never dropped.
            if result and used + size > MAX_INBOX_BYTES:
                break
            result.append(msg)
            used += size
        return result

    def inbox(self, *, peer_id: str, peek: bool = False) -> list[Message]:
        if peek:
            return self._select_unconsumed(peer_id=peer_id)

        # IMPORTANT: one atomic write transaction covering SELECT -> choose
        # ids -> mark exactly those ids consumed. BEGIN IMMEDIATE reserves
        # the write lock so two concurrent drains cannot both claim the same
        # rows; errors propagate to the caller (the factory rolls back).
        stamp = self._now()

        with self._connect(immediate=True) as db:
            rows = db.execute(
                """
                SELECT *
                FROM agent_messages
                WHERE to_peer=? AND consumed_at IS NULL
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
                    WHERE id=? AND consumed_at IS NULL
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

            # Second DB check closes:
            # query empty -> sender commit+notify -> waiter begins waiting.
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
        """Non-consuming peek at the oldest unconsumed message; used by unified wait."""
        messages = self._select_unconsumed(
            peer_id=peer_id,
            from_peer=from_peer,
            limit=1,
        )
        return messages[0] if messages else None

    def wait_surface(self, peer_id: str) -> tuple[threading.Condition, int]:
        """Expose the per-peer condition and current revision for external waiters (unified wait)."""
        state = self._wait_state(peer_id)
        with state.condition:
            return state.condition, state.revision

    def revision(self, peer_id: str) -> int:
        """Current mailbox revision for the peer."""
        state = self._wait_state(peer_id)
        with state.condition:
            return state.revision
