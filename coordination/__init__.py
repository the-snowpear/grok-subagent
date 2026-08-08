"""Coordination kernel package.

PR A only: registry + durable mailbox + main-facing hub.
"""

from .hub import CoordinationHub
from .mailbox import Mailbox
from .registry import AgentRegistry, main_peer_id

__all__ = ["AgentRegistry", "CoordinationHub", "Mailbox", "main_peer_id"]
