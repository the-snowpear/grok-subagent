"""Worker bridge CLI: let a Grok worker process talk to the observer daemon as its own identity.

A Grok worker spawned by the daemon receives three environment variables:

- GROK_OBSERVER_AGENT_ID: the worker's own agent id.
- GROK_OBSERVER_AGENT_TOKEN: the worker's hub token (authenticates worker actions).
- GROK_OBSERVER_WORKER_CONTROL_PORT: the worker control port (default 47832;
  falls back to GROK_OBSERVER_CONTROL_PORT for older daemons).

The CLI speaks the daemon's JSON-line worker control protocol: one request
line {"worker_id": ..., "worker_token": ..., **op args} in, one response line
{"ok": true, "data": ...} / {"ok": false, "error": ...} out.

Subcommands mirror the worker hub ops:

- peers            list the worker's thread peers (op list)
- send             send a message to the main peer or a sibling worker (op send)
- inbox            read (and drain) the worker's inbox (op inbox)
- wait             block until a message from a peer arrives (op wait)

Pure functions (build_request, request) are exposed for tests.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

DEFAULT_PORT = 47832
DEFAULT_TIMEOUT = 120
REQUEST_TIMEOUT = 15.0


def build_request(worker_id: str, worker_token: str, op_args: dict) -> dict:
    """Build the worker control request payload for the given op arguments."""
    return {
        "worker_id": worker_id,
        "worker_token": worker_token,
        **op_args,
    }


def request(port: int, payload: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
    """Send one JSON-line request to the daemon worker control port and read the response.

    Wait ops get a socket timeout that covers the requested wait duration;
    all other ops use the fixed REQUEST_TIMEOUT.

    Raises RuntimeError on connection failure or an empty reply.
    """
    if str(payload.get("op") or "").lower() == "wait":
        requested = int(payload.get("timeout_seconds", 120))
        timeout = max(10, requested + 5)
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(data)
            line = sock.makefile("r", encoding="utf-8").readline()
    except OSError as exc:
        raise RuntimeError(f"cannot reach daemon at 127.0.0.1:{port}: {exc}") from exc
    if not line:
        raise RuntimeError("empty reply from daemon")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid response from daemon: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser with the worker hub subcommands."""
    parser = argparse.ArgumentParser(
        prog="grok_hub",
        description="Worker bridge CLI for the Grok Agent Fabric observer daemon.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "daemon worker control port (default: $GROK_OBSERVER_WORKER_CONTROL_PORT, "
            f"else $GROK_OBSERVER_CONTROL_PORT, or {DEFAULT_PORT})"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("peers", help="list this worker's thread peers")

    send = subparsers.add_parser("send", help="send a message to a peer")
    send.add_argument("--to", required=True, help="target peer (main:<thread> or a worker id)")
    send.add_argument("--message", required=True, help="message body")
    send.add_argument("--reply-to", default=None, help="message id this is a reply to")

    inbox = subparsers.add_parser("inbox", help="read this worker's inbox")
    inbox.add_argument("--peek", action="store_true", help="read without draining")

    wait = subparsers.add_parser("wait", help="wait for a message from a peer")
    wait.add_argument("--from", dest="from_peer", default=None, help="only wait for messages from this peer")
    wait.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to wait (default: {DEFAULT_TIMEOUT})",
    )

    return parser


def main() -> None:
    """CLI entry point: parse args, call the daemon, print the result."""
    worker_id = os.environ.get("GROK_OBSERVER_AGENT_ID", "")
    worker_token = os.environ.get("GROK_OBSERVER_AGENT_TOKEN", "")
    if not worker_id or not worker_token:
        sys.exit("GROK_OBSERVER_AGENT_ID and GROK_OBSERVER_AGENT_TOKEN must be set")

    args = build_parser().parse_args()

    if args.command == "peers":
        op_args = {"op": "list"}
    elif args.command == "send":
        op_args = {"op": "send", "to": args.to, "message": args.message}
        if args.reply_to is not None:
            op_args["reply_to"] = args.reply_to
    elif args.command == "inbox":
        op_args = {"op": "inbox", "peek": bool(args.peek)}
    elif args.command == "wait":
        op_args = {"op": "wait", "timeout_seconds": int(args.timeout)}
        if args.from_peer is not None:
            op_args["from"] = args.from_peer
    else:  # pragma: no cover - argparse enforces the subcommand
        sys.exit(f"unknown command: {args.command}")

    port = args.port if args.port is not None else int(
        os.environ.get("GROK_OBSERVER_WORKER_CONTROL_PORT")
        or os.environ.get("GROK_OBSERVER_CONTROL_PORT")
        or DEFAULT_PORT
    )

    try:
        response = request(port, build_request(worker_id, worker_token, op_args))
    except (OSError, RuntimeError) as exc:
        print(f"grok_hub: {exc}", file=sys.stderr)
        sys.exit(2)

    if response.get("ok"):
        print(json.dumps(response.get("data"), ensure_ascii=False, indent=2))
        sys.exit(0)
    print(f"grok_hub: {response.get('error', 'unknown error')}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
