"""Feishu/Lark connector utility functions for chat and message operations."""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def list_chats(
    client: Any,
    page_size: int = 100,
    silent: bool = False,
) -> list[dict[str, Any]]:
    """List chats (group chats and P2P) from Feishu.

    Uses the Feishu IM List Chats API: GET /open-apis/im/v1/chats

    Args:
        client: lark_oapi.Client instance
        page_size: Number of chats per page (max 100)
        silent: If True, suppress progress output

    Returns:
        List of chat objects:
        [
            {
                "chat_id": "oc_xxx",
                "name": "My Group",
                "chat_type": "group",  # or "p2p"
                "owner_id": "ou_xxx",
                "description": "...",
            },
            ...
        ]
    """
    from lark_oapi.api.im.v1 import ListChatRequest

    if not silent:
        print(f"Fetching Feishu chats (page_size={page_size})...")

    chats: list[dict[str, Any]] = []
    page_token: str | None = None

    while True:
        try:
            max_retries = 3
            base_delay = 1.0
            response = None

            for retry in range(max_retries):
                try:
                    request_builder = ListChatRequest.builder().page_size(page_size)
                    if page_token:
                        request_builder = request_builder.page_token(page_token)
                    request = request_builder.build()

                    response = client.im.v1.chat.list(request)
                    break
                except Exception as e:
                    error_str = str(e)
                    if "rate" in error_str.lower() or "429" in error_str:
                        if retry < max_retries - 1:
                            delay = base_delay * (2**retry)
                            logger.warning(
                                "[LIST-CHATS] Rate limit hit, retrying in %.1fs (attempt %d/%d)",
                                delay,
                                retry + 1,
                                max_retries,
                            )
                            time.sleep(delay)
                        else:
                            raise
                    else:
                        raise

            if response is None or not response.success():
                error_msg = getattr(response, "msg", "unknown") if response else "no response"
                code = getattr(response, "code", -1) if response else -1
                raise Exception(f"Feishu API error (code={code}): {error_msg}")

            page_items = response.data.items or []
            for item in page_items:
                chats.append(
                    {
                        "chat_id": item.chat_id,
                        "name": item.name or item.chat_id,
                        "chat_type": getattr(item, "chat_type", "group"),
                        "owner_id": getattr(item, "owner_id", None),
                        "description": getattr(item, "description", ""),
                        "avatar": getattr(item, "avatar", None),
                    }
                )

            page_token = response.data.page_token
            if not page_token or response.data.has_more is False:
                break

        except Exception as e:
            logger.error("[LIST-CHATS] Error listing chats: %s", e)
            break

    if not silent:
        print(f"   Found {len(chats)} chats")

    return chats


def list_messages_from_chat(
    client: Any,
    chat_id: str,
    limit: int = 50,
    silent: bool = False,
) -> list[dict[str, Any]]:
    """List messages from a specific Feishu chat.

    Uses the Feishu IM List Messages API: GET /open-apis/im/v1/messages

    Args:
        client: lark_oapi.Client instance
        chat_id: Chat ID (e.g., "oc_xxx")
        limit: Maximum number of messages to fetch
        silent: If True, suppress progress output

    Returns:
        List of message objects:
        [
            {
                "message_id": "om_xxx",
                "msg_type": "text",
                "content": "...",
                "sender_id": "ou_xxx",
                "create_time": "1234567890000",
                "chat_id": "oc_xxx",
            },
            ...
        ]
    """
    from lark_oapi.api.im.v1 import ListMessageRequest

    if not silent:
        print(f"Fetching messages from chat {chat_id}...")

    messages: list[dict[str, Any]] = []
    page_token: str | None = None

    while True:
        try:
            max_retries = 3
            base_delay = 1.0
            response = None

            for retry in range(max_retries):
                try:
                    request_builder = (
                        ListMessageRequest.builder()
                        .container_id_type("chat")
                        .container_id(chat_id)
                        .page_size(min(50, limit - len(messages)))
                    )
                    if page_token:
                        request_builder = request_builder.page_token(page_token)
                    request = request_builder.build()

                    response = client.im.v1.message.list(request)
                    break
                except Exception as e:
                    error_str = str(e)
                    if "rate" in error_str.lower() or "429" in error_str:
                        if retry < max_retries - 1:
                            delay = base_delay * (2**retry)
                            logger.warning(
                                "[LIST-MESSAGES] Rate limit hit for chat %s, "
                                "retrying in %.1fs (attempt %d/%d)",
                                chat_id,
                                delay,
                                retry + 1,
                                max_retries,
                            )
                            time.sleep(delay)
                        else:
                            raise
                    else:
                        raise

            if response is None or not response.success():
                error_msg = getattr(response, "msg", "unknown") if response else "no response"
                code = getattr(response, "code", -1) if response else -1
                raise Exception(f"Feishu API error (code={code}): {error_msg}")

            page_items = response.data.items or []
            for item in page_items:
                sender = getattr(item, "sender", None)
                sender_id = getattr(sender, "id", None) if sender else None
                messages.append(
                    {
                        "message_id": item.message_id,
                        "msg_type": item.msg_type,
                        "content": item.body.content if item.body else "",
                        "sender_id": sender_id,
                        "create_time": item.create_time,
                        "chat_id": chat_id,
                    }
                )

            if len(messages) >= limit:
                messages = messages[:limit]
                break

            page_token = response.data.page_token
            if not page_token or response.data.has_more is False:
                break

        except Exception as e:
            logger.error("[LIST-MESSAGES] Error listing messages from chat %s: %s", chat_id, e)
            break

    if not silent:
        print(f"   Found {len(messages)} messages in chat {chat_id}")

    return messages


def send_message(
    client: Any,
    chat_id: str,
    msg_type: str,
    content: str,
) -> dict[str, Any]:
    """Send a message to a Feishu chat.

    Uses the Feishu IM Send Message API: POST /open-apis/im/v1/messages

    Args:
        client: lark_oapi.Client instance
        chat_id: Target chat ID (e.g., "oc_xxx")
        msg_type: Message type ("text", "interactive", "image", etc.)
        content: Message content as JSON string

    Returns:
        Sent message info dict with message_id

    Raises:
        Exception: If send fails
    """
    import json

    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    # Ensure content is a JSON string
    if isinstance(content, dict):
        content = json.dumps(content, ensure_ascii=False)

    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        .build()
    )

    response = client.im.v1.message.create(request)

    if not response.success():
        error_msg = getattr(response, "msg", "unknown")
        code = getattr(response, "code", -1)
        raise Exception(f"Feishu send message failed (code={code}): {error_msg}")

    result = response.data
    return {
        "message_id": result.message_id,
        "msg_type": getattr(result, "msg_type", msg_type),
        "create_time": getattr(result, "create_time", None),
    }


def send_p2p_message(
    client: Any,
    open_id: str,
    msg_type: str,
    content: str,
) -> dict[str, Any]:
    """Send a P2P message to a user by open_id.

    Creates or reuses a P2P chat between the bot and the user.

    Args:
        client: lark_oapi.Client instance
        open_id: Target user's open_id (e.g., "ou_xxx")
        msg_type: Message type ("text", "interactive", etc.)
        content: Message content as JSON string

    Returns:
        Sent message info dict with message_id and chat_id
    """
    import json as _json

    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    if isinstance(content, dict):
        content = _json.dumps(content, ensure_ascii=False)

    request = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        .build()
    )

    response = client.im.v1.message.create(request)

    if not response.success():
        error_msg = getattr(response, "msg", "unknown")
        code = getattr(response, "code", -1)
        raise Exception(f"Feishu send P2P message failed (code={code}): {error_msg}")

    result = response.data
    return {
        "message_id": result.message_id,
        "chat_id": getattr(result, "chat_id", None),
        "msg_type": getattr(result, "msg_type", msg_type),
        "create_time": getattr(result, "create_time", None),
    }


def get_chat_members(
    client: Any,
    chat_id: str,
) -> list[dict[str, Any]]:
    """Get members of a Feishu chat.

    Uses the Feishu IM Get Chat Members API.

    Args:
        client: lark_oapi.Client instance
        chat_id: Chat ID to look up members for

    Returns:
        List of member info dicts with name, member_id, member_id_type
    """
    from lark_oapi.api.im.v1 import GetChatMembersRequest

    try:
        request = GetChatMembersRequest.builder().chat_id(chat_id).page_size(50).build()
        response = client.im.v1.chat_members.get(request)

        if not response.success():
            logger.warning(
                "[GET-CHAT-MEMBERS] Failed for chat %s: code=%s msg=%s",
                chat_id,
                getattr(response, "code", -1),
                getattr(response, "msg", "unknown"),
            )
            return []

        members = []
        for item in response.data.items or []:
            members.append(
                {
                    "name": getattr(item, "name", "Unknown"),
                    "member_id": getattr(item, "member_id", None),
                    "member_id_type": getattr(item, "member_id_type", None),
                    "tenant_key": getattr(item, "tenant_key", None),
                }
            )
        return members

    except Exception as e:
        logger.warning("[GET-CHAT-MEMBERS] Error for chat %s: %s", chat_id, e)
        return []


def get_chat_info(
    client: Any,
    chat_id: str,
) -> dict[str, Any] | None:
    """Get detailed info about a Feishu chat.

    Uses the Feishu IM Get Chat API: GET /open-apis/im/v1/chats/:chat_id

    Args:
        client: lark_oapi.Client instance
        chat_id: Chat ID to look up

    Returns:
        Chat info dict or None if not found
    """
    from lark_oapi.api.im.v1 import GetChatRequest

    try:
        request = GetChatRequest.builder().chat_id(chat_id).build()
        response = client.im.v1.chat.get(request)

        if not response.success():
            logger.warning(
                "[GET-CHAT-INFO] Failed to get chat %s: code=%s msg=%s",
                chat_id,
                getattr(response, "code", -1),
                getattr(response, "msg", "unknown"),
            )
            return None

        item = response.data
        return {
            "chat_id": chat_id,
            "name": getattr(item, "name", None) or chat_id,
            "chat_type": getattr(item, "chat_type", "group"),
            "chat_mode": getattr(item, "chat_mode", "group"),
            "owner_id": getattr(item, "owner_id", None),
            "description": getattr(item, "description", ""),
        }

    except Exception as e:
        logger.warning("[GET-CHAT-INFO] Error fetching chat %s: %s", chat_id, e)
        return None
