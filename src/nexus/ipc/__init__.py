"""Filesystem-as-IPC brick for agent-to-agent communication.

Implements inbox/outbox conventions, message envelopes, agent discovery,
and delivery guarantees on top of existing Nexus kernel primitives
(VFS Router, EventBus, Agent Registry, ReBAC).

Issue: #1411
Architecture: NEXUS-LEGO-ARCHITECTURE.md Part 17, sections 17.3-17.5

Usage:
    from nexus.ipc import MessageEnvelope, MessageSender, MessageProcessor
    from nexus.ipc.conventions import inbox_path, outbox_path
    from nexus.ipc.discovery import AgentDiscovery
"""

from nexus.ipc.conventions import (
    AGENTS_ROOT,
    agent_dir,
    dead_letter_path,
    inbox_path,
    message_filename,
    outbox_path,
    processed_path,
)
from nexus.ipc.delivery import MessageProcessor, MessageSender
from nexus.ipc.discovery import AgentDiscovery
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.exceptions import (
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
    IPCError,
    MessageExpiredError,
)
from nexus.ipc.provisioning import AgentProvisioner
from nexus.ipc.sweep import TTLSweeper

__all__ = [
    # Envelope
    "MessageEnvelope",
    "MessageType",
    # Conventions
    "AGENTS_ROOT",
    "agent_dir",
    "inbox_path",
    "outbox_path",
    "processed_path",
    "dead_letter_path",
    "message_filename",
    # Delivery
    "MessageSender",
    "MessageProcessor",
    # Discovery
    "AgentDiscovery",
    # Provisioning
    "AgentProvisioner",
    # Sweep
    "TTLSweeper",
    # Exceptions
    "IPCError",
    "EnvelopeValidationError",
    "InboxNotFoundError",
    "InboxFullError",
    "MessageExpiredError",
]
