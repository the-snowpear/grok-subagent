"""Typed data contracts for the coordination kernel."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

PeerKind = Literal["main", "worker", "advisor"]
MessageState = Literal["pending", "delivered", "consumed", "failed"]

MAX_MESSAGE_BYTES = 64 * 1024
MAX_INBOX_MESSAGES = 100
MAX_INBOX_BYTES = 256 * 1024


@dataclass(frozen=True)
class Peer:
    id: str
    thread_id: str
    name: str
    display_title: str
    kind: PeerKind
    status: str
    updated_at: str
    role: str | None = None
    activity: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Message:
    id: str
    thread_id: str
    from_peer: str
    to_peer: str
    kind: str
    body: str
    reply_to: str | None
    delivery_mode: str
    state: MessageState
    target_turn_id: int | None
    error: str | None
    created_at: str
    delivered_at: str | None
    consumed_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)
