"""Filesystem-as-IPC brick for agent-to-agent communication.

Implements inbox/outbox conventions, message envelopes, agent discovery,
delivery guarantees, and pluggable storage drivers on top of existing
Nexus kernel primitives (VFS Router, EventBus, Agent Registry, ReBAC).

IPC operations go through the kernel VFS (NexusFS) directly, gaining
PathRouter routing, ReBAC permission checks, MetastoreABC metadata
tracking, EventLog auditing, content caching, and Raft replication.

Issues: #1411, #1243, #1178
Architecture: KERNEL-ARCHITECTURE.md

Usage:
    from nexus.bricks.ipc import MessageEnvelope, MessageSender, MessageProcessor
    from nexus.bricks.ipc.conventions import inbox_path, outbox_path
    from nexus.bricks.ipc.discovery import AgentDiscovery
"""

from nexus.bricks.ipc.conventions import (
    AGENTS_ROOT,
    agent_dir,
    dead_letter_path,
    inbox_path,
    message_filename,
    notify_pipe_path,
    outbox_path,
    processed_path,
)
from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
from nexus.bricks.ipc.discovery import AgentDiscovery
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.exceptions import (
    CrossZoneDeliveryError,
    DLQReason,
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
    IPCError,
    MessageExpiredError,
)
from nexus.bricks.ipc.lifecycle import dead_letter_message
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.ipc.signing import MessageSigner, MessageVerifier, SigningMode, VerifyResult
from nexus.bricks.ipc.sweep import TTLSweeper
from nexus.bricks.ipc.wakeup import (
    CacheStoreEventPublisher,
    PipeNotifyFactory,
    PipeWakeupListener,
    PipeWakeupNotifier,
)

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
    "notify_pipe_path",
    "message_filename",
    # Delivery
    "MessageSender",
    "MessageProcessor",
    # Signing (#1729)
    "MessageSigner",
    "MessageVerifier",
    "SigningMode",
    "VerifyResult",
    # Discovery
    "AgentDiscovery",
    # Provisioning
    "AgentProvisioner",
    # Sweep
    "TTLSweeper",
    # Lifecycle
    "dead_letter_message",
    # Wakeup (#3197)
    "PipeWakeupNotifier",
    "PipeWakeupListener",
    "PipeNotifyFactory",
    # Exceptions
    "IPCError",
    "EnvelopeValidationError",
    "InboxNotFoundError",
    "InboxFullError",
    "MessageExpiredError",
    "CrossZoneDeliveryError",
    "DLQReason",
]
