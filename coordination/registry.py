"""Thread-scoped peer registry.

Important invariant:
A cross-thread worker MUST resolve exactly like an unknown worker.
Do not query by id first and later disclose its thread.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager as ContextManager
from sqlite3 import Connection

from .types import Peer

ConnectFactory = Callable[[], ContextManager[Connection]]


def main_peer_id(thread_id: str) -> str:
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread_id is required")
    return f"main:{thread_id}"


class AgentRegistry:
    def __init__(self, connect_factory: ConnectFactory):
        self._connect = connect_factory

    def list_workers(self, thread_id: str) -> list[Peer]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT
                    id,
                    thread_id,
                    name,
                    COALESCE(display_title, '') AS display_title,
                    status,
                    updated_at
                FROM agents
                WHERE thread_id=?
                ORDER BY updated_at DESC, id ASC
                """,
                (thread_id,),
            ).fetchall()

        peers: list[Peer] = []
        for row in rows:
            peers.append(
                Peer(
                    id=str(row["id"]),
                    thread_id=str(row["thread_id"]),
                    name=str(row["name"] or ""),
                    display_title=str(row["display_title"] or ""),
                    kind="worker",
                    status=str(row["status"] or ""),
                    updated_at=str(row["updated_at"] or ""),
                )
            )
        return peers

    def resolve_worker(self, thread_id: str, agent_id: str) -> Peer | None:
        """Resolve only inside the caller's thread.

        Cross-thread and unknown ids both return None.
        """
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            return None

        with self._connect() as db:
            row = db.execute(
                """
                SELECT
                    id,
                    thread_id,
                    name,
                    COALESCE(display_title, '') AS display_title,
                    status,
                    updated_at
                FROM agents
                WHERE id=? AND thread_id=?
                """,
                (agent_id, thread_id),
            ).fetchone()

        if not row:
            return None

        return Peer(
            id=str(row["id"]),
            thread_id=str(row["thread_id"]),
            name=str(row["name"] or ""),
            display_title=str(row["display_title"] or ""),
            kind="worker",
            status=str(row["status"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )
