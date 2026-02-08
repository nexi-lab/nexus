"""Conversation utilities for message storage.

Uses NexusFS for file operations - no database needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.message_gateway.types import Message

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


def get_conversation_path(session_id: str) -> str:
    """Get the file path for a session's conversation.

    Args:
        session_id: Session key (e.g., "discord:guild_1:chan_1")

    Returns:
        Path to conversation.jsonl file
    """
    # Sanitize session_id for path safety
    safe_id = session_id.replace("/", "_").replace("..", "_")
    return f"/sessions/{safe_id}/conversation.jsonl"


def append_message(
    nx: NexusFS,
    session_id: str,
    message: Message,
    context: OperationContext,
) -> None:
    """Append a message to the conversation file.

    Creates the file if it doesn't exist (NexusFS append semantics).

    Args:
        nx: NexusFS instance
        session_id: Session key
        message: Message to append
        context: Operation context for permissions (required)
    """
    path = get_conversation_path(session_id)
    content = message.to_jsonl() + "\n"

    try:
        nx.append(path, content, context=context)
        logger.debug(f"Appended message {message.id} to {path}")
    except Exception as e:
        logger.error(f"Failed to append message to {path}: {e}")
        raise


def read_messages(
    nx: NexusFS,
    session_id: str,
    context: OperationContext,
) -> list[Message]:
    """Read all messages from a conversation file.

    Args:
        nx: NexusFS instance
        session_id: Session key
        context: Operation context for permissions (required)

    Returns:
        List of messages, oldest first
    """
    path = get_conversation_path(session_id)

    try:
        if not nx.exists(path, context=context):
            return []

        raw_content = nx.read(path, context=context)
        if isinstance(raw_content, bytes):
            text_content = raw_content.decode("utf-8")
        elif isinstance(raw_content, str):
            text_content = raw_content
        else:
            # Dict result from parsed content - shouldn't happen for JSONL
            logger.warning(f"Unexpected content type from {path}: {type(raw_content)}")
            return []

        messages = []
        for line_num, line in enumerate(text_content.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(Message.from_jsonl(line))
            except Exception as e:
                logger.warning(f"Failed to parse line {line_num} in {path}: {e}")
                continue

        return messages

    except Exception as e:
        logger.error(f"Failed to read messages from {path}: {e}")
        raise
