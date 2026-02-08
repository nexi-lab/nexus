"""Message Gateway package.

Unified message handling for human/agent communication.
"""

from nexus.message_gateway.channel_adapter import ChannelAdapter
from nexus.message_gateway.client import GatewayClient, GatewayError
from nexus.message_gateway.conversation import (
    append_message,
    cleanup_orphan_messages,
    ensure_session_metadata,
    get_conversation_path,
    get_sync_cursor,
    is_channel_native_id,
    read_messages,
    read_session_metadata,
    sync_messages,
    update_sync_cursor,
    write_session_metadata,
)
from nexus.message_gateway.dedup import Deduplicator
from nexus.message_gateway.session_router import derive_session_key, parse_session_key
from nexus.message_gateway.types import Message
from nexus.message_gateway.watcher import ConversationWatcher

__all__ = [
    "ChannelAdapter",
    "ConversationWatcher",
    "Deduplicator",
    "GatewayClient",
    "GatewayError",
    "Message",
    "append_message",
    "cleanup_orphan_messages",
    "derive_session_key",
    "ensure_session_metadata",
    "get_conversation_path",
    "get_sync_cursor",
    "is_channel_native_id",
    "parse_session_key",
    "read_messages",
    "read_session_metadata",
    "sync_messages",
    "update_sync_cursor",
    "write_session_metadata",
]
