"""Message Gateway package.

Unified message handling for human/agent communication.
"""

from nexus.message_gateway.channel_adapter import ChannelAdapter
from nexus.message_gateway.conversation import append_message, read_messages
from nexus.message_gateway.dedup import Deduplicator
from nexus.message_gateway.session_router import derive_session_key, parse_session_key
from nexus.message_gateway.types import Message

__all__ = [
    "ChannelAdapter",
    "Deduplicator",
    "Message",
    "append_message",
    "derive_session_key",
    "parse_session_key",
    "read_messages",
]
