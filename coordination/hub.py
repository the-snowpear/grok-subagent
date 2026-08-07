"""Main-facing coordination hub."""

from __future__ import annotations

from .mailbox import Mailbox
from .registry import AgentRegistry, main_peer_id


class CoordinationHub:
    def __init__(self, registry: AgentRegistry, mailbox: Mailbox):
        self._registry = registry
        self._mailbox = mailbox

    def handle_main(self, *, thread_id: str, args: dict) -> dict:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            raise ValueError("codex_thread_id is required")

        caller = main_peer_id(thread_id)
        op = str(args.get("op") or "").strip().lower()

        if op == "list":
            return {
                "caller": caller,
                "peers": [p.to_dict() for p in self._registry.list_workers(thread_id)],
            }

        if op == "send":
            target_id = str(args.get("to") or "").strip()
            target = self._registry.resolve_worker(thread_id, target_id)
            if target is None:
                raise ValueError("peer not found")

            reply_to = args.get("reply_to")
            if reply_to is not None and not isinstance(reply_to, str):
                raise ValueError("reply_to must be a string")

            msg = self._mailbox.send(
                thread_id=thread_id,
                from_peer=caller,
                to_peer=target.id,
                body=args.get("message"),
                reply_to=reply_to,
            )
            return {
                "message_id": msg.id,
                "from": msg.from_peer,
                "to": msg.to_peer,
                "state": msg.state,
            }

        if op == "inbox":
            peek = bool(args.get("peek", False))
            messages = self._mailbox.inbox(peer_id=caller, peek=peek)
            return {
                "caller": caller,
                "peek": peek,
                "messages": [m.to_dict() for m in messages],
            }

        if op == "wait":
            from_peer = args.get("from")
            if from_peer is not None:
                from_peer = str(from_peer).strip()
                if from_peer and from_peer != caller:
                    # Do not leak cross-thread identities.
                    if self._registry.resolve_worker(thread_id, from_peer) is None:
                        raise ValueError("peer not found")
                elif not from_peer:
                    from_peer = None

            timeout = args.get("timeout_seconds", 120)
            try:
                timeout = int(timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("timeout_seconds must be an integer") from exc
            timeout = min(max(timeout, 0), 300)

            msg = self._mailbox.wait(
                peer_id=caller,
                from_peer=from_peer,
                timeout_seconds=timeout,
            )
            if msg is None:
                return {"kind": "timeout"}
            return {"kind": "message", "message": msg.to_dict()}

        raise ValueError("op must be one of: list, send, inbox, wait")

    def handle_worker(self, *, worker_id: str, args: dict) -> dict:
        """Worker-facing hub ops; token authentication happens in the daemon.

        Identity is resolved via the worker's own id only (self-identity);
        this must never be used for cross-thread discovery.
        """
        worker_id = str(worker_id or "").strip()
        peer = self._registry.worker_peer(worker_id)
        if peer is None:
            raise ValueError("worker not found")
        thread_id = peer.thread_id

        op = str(args.get("op") or "").strip().lower()

        if op == "list":
            return {
                "caller": worker_id,
                "main": main_peer_id(thread_id),
                "peers": [p.to_dict() for p in self._registry.list_workers(thread_id)],
            }

        if op == "send":
            main = main_peer_id(thread_id)
            target_id = str(args.get("to") or "").strip()
            if target_id == main:
                to_peer = main
            else:
                target = self._registry.resolve_worker(thread_id, target_id)
                if target is None:
                    raise ValueError("peer not found")
                to_peer = target.id

            reply_to = args.get("reply_to")
            if reply_to is not None and not isinstance(reply_to, str):
                raise ValueError("reply_to must be a string")

            msg = self._mailbox.send(
                thread_id=thread_id,
                from_peer=worker_id,
                to_peer=to_peer,
                body=args.get("message"),
                reply_to=reply_to,
            )
            return {
                "message_id": msg.id,
                "from": msg.from_peer,
                "to": msg.to_peer,
                "state": msg.state,
            }

        if op == "inbox":
            peek = bool(args.get("peek", False))
            messages = self._mailbox.inbox(peer_id=worker_id, peek=peek)
            return {
                "caller": worker_id,
                "peek": peek,
                "messages": [m.to_dict() for m in messages],
            }

        if op == "wait":
            from_peer = args.get("from")
            if from_peer is not None:
                from_peer = str(from_peer).strip()
                if not from_peer:
                    from_peer = None
                elif from_peer != main_peer_id(thread_id):
                    target = self._registry.resolve_worker(thread_id, from_peer)
                    if target is None:
                        raise ValueError("peer not found")
                    from_peer = target.id

            timeout = args.get("timeout_seconds", 120)
            try:
                timeout = int(timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("timeout_seconds must be an integer") from exc
            timeout = min(max(timeout, 0), 300)

            msg = self._mailbox.wait(
                peer_id=worker_id,
                from_peer=from_peer,
                timeout_seconds=timeout,
            )
            if msg is None:
                return {"kind": "timeout"}
            return {"kind": "message", "message": msg.to_dict()}

        raise ValueError("op must be one of: list, send, inbox, wait")
