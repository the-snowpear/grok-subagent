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
