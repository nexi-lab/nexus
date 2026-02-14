"""Filesystem-as-IPC brick for agent-to-agent communication.

Implements inbox/outbox conventions, message envelopes, agent discovery,
delivery guarantees, and pluggable storage drivers on top of existing
Nexus kernel primitives (VFS Router, EventBus, Agent Registry, ReBAC).

Issues: #1411, #1243
Architecture: KERNEL-ARCHITECTURE.md

Usage:
    from nexus.ipc import MessageEnvelope, MessageSender, MessageProcessor
    from nexus.ipc.conventions import inbox_path, outbox_path
    from nexus.ipc.discovery import AgentDiscovery
    from nexus.ipc.storage import IPCStorageDriver
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
from nexus.ipc.driver import IPCVFSDriver
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
    # VFS Driver
    "IPCVFSDriver",
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
