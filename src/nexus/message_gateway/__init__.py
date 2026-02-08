"""Message Gateway package.

Unified message handling for human/agent communication.
"""

from nexus.message_gateway.conversation import append_message, read_messages
from nexus.message_gateway.dedup import Deduplicator
from nexus.message_gateway.types import Message

__all__ = [
    "Message",
    "append_message",
    "read_messages",
    "Deduplicator",
]
