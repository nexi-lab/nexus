"""Conversation utilities for message storage.

Uses NexusFS for file operations - no database needed.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.message_gateway.types import Message

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


def _call_with_context(method: Any, *args: Any, context: Any = None, **kwargs: Any) -> Any:
    """Call a NexusFS method, handling both local and remote clients.

    Local NexusFS requires context for permissions.
    RemoteNexusFS handles auth via API key and doesn't accept context.
    """
    try:
        return method(*args, context=context, **kwargs)
    except TypeError as e:
        if "context" in str(e):
            # RemoteNexusFS doesn't accept context, retry without it
            return method(*args, **kwargs)
        raise


def _get_session_dir(session_id: str) -> str:
    """Get the directory path for a session.

    Args:
        session_id: Session key (e.g., "discord:guild_1:chan_1")

    Returns:
        Path to session directory
    """
    safe_id = session_id.replace("/", "_").replace("..", "_")
    return f"/sessions/{safe_id}"


def get_conversation_path(session_id: str) -> str:
    """Get the file path for a session's conversation.

    Args:
        session_id: Session key (e.g., "discord:guild_1:chan_1")

    Returns:
        Path to conversation.jsonl file
    """
    return f"{_get_session_dir(session_id)}/conversation.jsonl"


def get_metadata_path(session_id: str) -> str:
    """Get the file path for a session's metadata.

    Args:
        session_id: Session key (e.g., "discord:guild_1:chan_1")

    Returns:
        Path to metadata.json file
    """
    return f"{_get_session_dir(session_id)}/metadata.json"


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
        if not _call_with_context(nx.exists, path, context=context):
            return []

        raw_content = _call_with_context(nx.read, path, context=context)
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


def write_session_metadata(
    nx: NexusFS,
    session_id: str,
    metadata: dict[str, Any],
    context: OperationContext,
) -> None:
    """Write session metadata to metadata.json.

    Args:
        nx: NexusFS instance
        session_id: Session key
        metadata: Metadata dict (e.g., guild_name, channel_name)
        context: Operation context for permissions (required)
    """
    path = get_metadata_path(session_id)
    content = json.dumps(metadata, indent=2)

    try:
        _call_with_context(nx.write, path, content.encode("utf-8"), context=context)
        logger.debug(f"Wrote session metadata to {path}")
    except Exception as e:
        logger.error(f"Failed to write session metadata to {path}: {e}")
        raise


def read_session_metadata(
    nx: NexusFS,
    session_id: str,
    context: OperationContext,
) -> dict[str, Any] | None:
    """Read session metadata from metadata.json.

    Args:
        nx: NexusFS instance
        session_id: Session key
        context: Operation context for permissions (required)

    Returns:
        Metadata dict or None if not found
    """
    path = get_metadata_path(session_id)

    try:
        if not _call_with_context(nx.exists, path, context=context):
            return None

        raw_content = _call_with_context(nx.read, path, context=context)
        if isinstance(raw_content, bytes):
            text_content = raw_content.decode("utf-8")
        elif isinstance(raw_content, str):
            text_content = raw_content
        else:
            logger.warning(f"Unexpected content type from {path}: {type(raw_content)}")
            return None

        return json.loads(text_content)

    except Exception as e:
        logger.error(f"Failed to read session metadata from {path}: {e}")
        return None


def ensure_session_metadata(
    nx: NexusFS,
    session_id: str,
    metadata: dict[str, Any],
    context: OperationContext,
) -> None:
    """Ensure session metadata exists, creating if needed.

    Only writes if metadata.json doesn't exist yet.

    Args:
        nx: NexusFS instance
        session_id: Session key
        metadata: Metadata dict to write if file doesn't exist
        context: Operation context for permissions (required)
    """
    path = get_metadata_path(session_id)

    try:
        if not _call_with_context(nx.exists, path, context=context):
            write_session_metadata(nx, session_id, metadata, context)
            logger.info(f"Created session metadata for {session_id}")
    except Exception as e:
        # Non-fatal - log and continue
        logger.warning(f"Failed to ensure session metadata for {session_id}: {e}")


def get_sync_cursor(
    nx: NexusFS,
    session_id: str,
    context: OperationContext,
) -> dict[str, str] | None:
    """Get the sync cursor from session metadata.

    Returns the last synced message ID and timestamp, used for
    incremental sync (fetch only messages after last sync).

    Args:
        nx: NexusFS instance
        session_id: Session key
        context: Operation context for permissions (required)

    Returns:
        Dict with 'last_synced_id' and 'last_synced_ts', or None if not set
    """
    metadata = read_session_metadata(nx, session_id, context)
    if not metadata:
        return None

    last_id = metadata.get("last_synced_id")
    last_ts = metadata.get("last_synced_ts")

    if last_id and last_ts:
        return {"last_synced_id": last_id, "last_synced_ts": last_ts}
    return None


def update_sync_cursor(
    nx: NexusFS,
    session_id: str,
    last_message: Message,
    context: OperationContext,
) -> None:
    """Update the sync cursor in session metadata.

    Stores the last synced message ID and timestamp for incremental sync.

    Args:
        nx: NexusFS instance
        session_id: Session key
        last_message: The last message that was synced
        context: Operation context for permissions (required)
    """
    # Read existing metadata or create new
    metadata = read_session_metadata(nx, session_id, context) or {}

    # Update sync cursor
    metadata["last_synced_id"] = last_message.id
    metadata["last_synced_ts"] = last_message.ts

    # Write back
    try:
        write_session_metadata(nx, session_id, metadata, context)
        logger.debug(f"Updated sync cursor for {session_id}: id={last_message.id}")
    except Exception as e:
        logger.warning(f"Failed to update sync cursor for {session_id}: {e}")


def sync_messages(
    nx: NexusFS,
    session_id: str,
    messages: list[Message],
    context: OperationContext,
    *,
    update_cursor: bool = True,
) -> tuple[int, int]:
    """Sync messages to conversation file, skipping duplicates.

    Messages are identified by their `id` field (channel's native message ID).
    Existing messages are skipped, new messages are appended in order.

    After syncing, updates the sync cursor in metadata with the last
    message's ID and timestamp (for incremental sync).

    Args:
        nx: NexusFS instance
        session_id: Session key
        messages: List of messages to sync (should be in chronological order)
        context: Operation context for permissions (required)
        update_cursor: Whether to update the sync cursor after sync (default: True)

    Returns:
        Tuple of (added_count, skipped_count)
    """
    if not messages:
        return 0, 0

    # Read existing message IDs
    existing_messages = read_messages(nx, session_id, context)
    existing_ids = {msg.id for msg in existing_messages}

    added = 0
    skipped = 0
    last_added: Message | None = None

    for message in messages:
        if message.id in existing_ids:
            skipped += 1
            continue

        # Append new message
        try:
            append_message(nx, session_id, message, context)
            existing_ids.add(message.id)  # Track for duplicates within batch
            added += 1
            last_added = message
        except Exception as e:
            logger.error(f"Failed to sync message {message.id}: {e}")
            # Continue with other messages

    # Update sync cursor with the last message (newest in chronological order)
    if update_cursor and messages:
        # Use the last message in the list (newest) as the cursor
        update_sync_cursor(nx, session_id, messages[-1], context)

    logger.info(f"Synced {session_id}: added={added}, skipped={skipped}")
    return added, skipped
