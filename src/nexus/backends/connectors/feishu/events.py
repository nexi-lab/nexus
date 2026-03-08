"""Shared Feishu-to-FileEvent translator.

Used by the WebSocket worker to map inbound Feishu events to Nexus
FileEvents for downstream processing.

Event mapping:
    im.message.receive_v1      -> FILE_WRITE  /chat/feishu/{groups|p2p}/{chat_id}.yaml
    im.chat.member.bot.added_v1   -> DIR_CREATE  /chat/feishu/groups/{chat_id}/
    im.chat.member.bot.deleted_v1 -> DIR_DELETE  /chat/feishu/groups/{chat_id}/
"""

import json
import logging
from typing import Any

from nexus.core.file_events import FileEvent, FileEventType

logger = logging.getLogger(__name__)

# VFS mount prefix for all Feishu paths
FEISHU_MOUNT_PREFIX = "/chat/feishu"


def translate_feishu_event(
    event_type: str,
    event: dict[str, Any],
) -> FileEvent | None:
    """Map a Feishu event to a Nexus FileEvent.

    This is the single source of truth for event-to-path mapping.

    Args:
        event_type: Feishu event type string (e.g. "im.message.receive_v1")
        event: Event payload dict from Feishu

    Returns:
        FileEvent or None if the event type is not mapped
    """
    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        chat_id = message.get("chat_id", "unknown")
        chat_type = message.get("chat_type", "group")
        folder = "p2p" if chat_type == "p2p" else "groups"
        return FileEvent(
            type=FileEventType.FILE_WRITE,
            path=f"{FEISHU_MOUNT_PREFIX}/{folder}/{chat_id}.yaml",
            size=len(json.dumps(message)),
        )

    if event_type == "im.chat.member.bot.added_v1":
        chat_id = event.get("chat_id", "unknown")
        return FileEvent(
            type=FileEventType.DIR_CREATE,
            path=f"{FEISHU_MOUNT_PREFIX}/groups/{chat_id}/",
        )

    if event_type == "im.chat.member.bot.deleted_v1":
        chat_id = event.get("chat_id", "unknown")
        return FileEvent(
            type=FileEventType.DIR_DELETE,
            path=f"{FEISHU_MOUNT_PREFIX}/groups/{chat_id}/",
        )

    logger.debug("Unmapped Feishu event type: %s", event_type)
    return None
