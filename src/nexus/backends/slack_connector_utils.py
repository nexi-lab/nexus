"""Slack connector utility functions for message fetching and organization."""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def list_channels(
    client: Any,
    types: str = "public_channel,private_channel",
    exclude_archived: bool = True,
    limit: int | None = None,
    silent: bool = False,
) -> list[dict[str, Any]]:
    """List channels from Slack workspace.

    Args:
        client: Slack API client (slack_sdk.WebClient)
        types: Comma-separated list of channel types to include
               (public_channel, private_channel, mpim, im)
        exclude_archived: Whether to exclude archived channels
        limit: Maximum number of channels to fetch (None for all)
        silent: If True, suppress progress output

    Returns:
        List of channel objects:
        [
            {
                "id": "C1234567890",
                "name": "general",
                "is_channel": true,
                "is_private": false,
                "is_archived": false,
                "num_members": 10
            },
            ...
        ]
    """
    if not silent:
        logger.info("Fetching channels (types: %s)", types)

    channels = []
    cursor = None

    while True:
        try:
            # Use exponential backoff for rate limiting
            max_retries = 5
            base_delay = 1.0
            result = None

            for retry in range(max_retries):
                try:
                    result = client.conversations_list(
                        types=types,
                        exclude_archived=exclude_archived,
                        limit=200,  # Max per page
                        cursor=cursor,
                    )
                    break  # Success
                except Exception as e:
                    error_str = str(e)
                    if "rate_limited" in error_str or "429" in error_str:
                        if retry < max_retries - 1:
                            delay = base_delay * (2**retry)
                            logger.warning(
                                f"[LIST-CHANNELS] Rate limit hit, retrying in {delay}s "
                                f"(attempt {retry + 1}/{max_retries})"
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"[LIST-CHANNELS] Rate limit exceeded after {max_retries} retries"
                            )
                            raise
                    else:
                        logger.error(f"[LIST-CHANNELS] Failed to list channels: {e}")
                        raise

            if result is None or not result.get("ok"):
                error = result.get("error", "unknown_error") if result else "unknown_error"
                raise Exception(f"Slack API error: {error}")

            page_channels = result.get("channels", [])
            channels.extend(page_channels)

            if limit and len(channels) >= limit:
                channels = channels[:limit]
                break

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        except Exception as e:
            logger.error(f"[LIST-CHANNELS] Error listing channels: {e}")
            break

    if not silent:
        logger.info("Found %d channels", len(channels))

    return channels


def list_messages_from_channel(
    client: Any,
    channel_id: str,
    channel_name: str,
    limit: int | None = 100,
    oldest: str | None = None,
    latest: str | None = None,
    silent: bool = False,
) -> list[dict[str, Any]]:
    """List messages from a specific Slack channel.

    Args:
        client: Slack API client (slack_sdk.WebClient)
        channel_id: Channel ID (e.g., "C1234567890")
        channel_name: Channel name (for logging)
        limit: Maximum number of messages to fetch (None for all)
        oldest: Only messages after this Unix timestamp (inclusive)
        latest: Only messages before this Unix timestamp (exclusive)
        silent: If True, suppress progress output

    Returns:
        List of message objects with channel context:
        [
            {
                "type": "message",
                "user": "U1234567890",
                "text": "Hello world",
                "ts": "1234567890.123456",
                "channel_id": "C1234567890",
                "channel_name": "general",
                "thread_ts": "1234567890.123456",  # If threaded message
                "reply_count": 5  # If has replies
            },
            ...
        ]
    """
    if not silent:
        logger.info("Fetching messages from #%s", channel_name)

    messages = []
    cursor = None

    while True:
        try:
            # Use exponential backoff for rate limiting
            max_retries = 5
            base_delay = 1.0
            result = None

            for retry in range(max_retries):
                try:
                    params = {
                        "channel": channel_id,
                        "limit": 200,  # Max per page
                    }
                    if cursor:
                        params["cursor"] = cursor
                    if oldest:
                        params["oldest"] = oldest
                    if latest:
                        params["latest"] = latest

                    result = client.conversations_history(**params)
                    break  # Success
                except Exception as e:
                    error_str = str(e)
                    if "rate_limited" in error_str or "429" in error_str:
                        if retry < max_retries - 1:
                            delay = base_delay * (2**retry)
                            logger.warning(
                                f"[LIST-MESSAGES] Rate limit hit for #{channel_name}, "
                                f"retrying in {delay}s (attempt {retry + 1}/{max_retries})"
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"[LIST-MESSAGES] Rate limit exceeded after {max_retries} retries"
                            )
                            raise
                    else:
                        logger.error(f"[LIST-MESSAGES] Failed to list messages: {e}")
                        raise

            if result is None or not result.get("ok"):
                error = result.get("error", "unknown_error") if result else "unknown_error"
                raise Exception(f"Slack API error: {error}")

            page_messages = result.get("messages", [])

            # Add channel context to each message
            for msg in page_messages:
                msg["channel_id"] = channel_id
                msg["channel_name"] = channel_name

            messages.extend(page_messages)

            if limit and len(messages) >= limit:
                messages = messages[:limit]
                break

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        except Exception as e:
            logger.error(f"[LIST-MESSAGES] Error listing messages from #{channel_name}: {e}")
            break

    if not silent:
        logger.info("Found %d messages in #%s", len(messages), channel_name)

    return messages


def list_thread_replies(
    client: Any,
    channel_id: str,
    thread_ts: str,
    silent: bool = False,
) -> list[dict[str, Any]]:
    """List replies in a message thread.

    Args:
        client: Slack API client (slack_sdk.WebClient)
        channel_id: Channel ID containing the thread
        thread_ts: Timestamp of the parent message
        silent: If True, suppress progress output

    Returns:
        List of reply messages (includes parent message as first item)
    """
    try:
        result = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
        )

        if not result.get("ok"):
            error = result.get("error", "unknown_error")
            logger.error(f"[LIST-THREAD-REPLIES] Slack API error: {error}")
            return []

        messages = result.get("messages", [])

        if not silent and len(messages) > 1:
            logger.info("Found %d replies in thread %s", len(messages) - 1, thread_ts)

        return messages

    except Exception as e:
        logger.error(f"[LIST-THREAD-REPLIES] Error fetching thread replies: {e}")
        return []


def get_user_info(
    client: Any,
    user_id: str,
    user_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Get user information from Slack.

    Args:
        client: Slack API client (slack_sdk.WebClient)
        user_id: User ID (e.g., "U1234567890")
        user_cache: Optional cache dict to store user info

    Returns:
        User info dict or None if not found:
        {
            "id": "U1234567890",
            "name": "johndoe",
            "real_name": "John Doe",
            "profile": {
                "email": "john@example.com",
                "display_name": "John",
                ...
            }
        }
    """
    # Check cache first
    if user_cache is not None and user_id in user_cache:
        return user_cache[user_id]

    try:
        result = client.users_info(user=user_id)

        if not result.get("ok"):
            error = result.get("error", "unknown_error")
            logger.warning(f"[GET-USER-INFO] Failed to get user {user_id}: {error}")
            return None

        user_info = result.get("user")

        # Cache it
        if user_cache is not None and user_info:
            user_cache[user_id] = user_info

        return user_info

    except Exception as e:
        logger.warning(f"[GET-USER-INFO] Error fetching user {user_id}: {e}")
        return None


def fetch_messages_batch(
    client: Any,
    channels: list[dict[str, Any]],
    max_messages_per_channel: int | None = 100,
    include_threads: bool = False,
    silent: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch messages from multiple channels in batch.

    Args:
        client: Slack API client (slack_sdk.WebClient)
        channels: List of channel objects from list_channels()
        max_messages_per_channel: Max messages to fetch per channel
        include_threads: Whether to fetch thread replies
        silent: If True, suppress progress output

    Returns:
        Dict mapping channel_id -> list of messages
    """
    messages_by_channel: dict[str, list[dict[str, Any]]] = {}

    for channel in channels:
        channel_id = channel["id"]
        channel_name = channel.get("name", channel_id)

        messages = list_messages_from_channel(
            client=client,
            channel_id=channel_id,
            channel_name=channel_name,
            limit=max_messages_per_channel,
            silent=silent,
        )

        # Fetch thread replies if requested
        if include_threads:
            for msg in messages:
                if msg.get("reply_count", 0) > 0 and "thread_ts" in msg:
                    thread_replies = list_thread_replies(
                        client=client,
                        channel_id=channel_id,
                        thread_ts=msg["thread_ts"],
                        silent=silent,
                    )
                    # Add thread replies to message
                    msg["thread_replies"] = thread_replies[1:]  # Exclude parent

        messages_by_channel[channel_id] = messages

    return messages_by_channel


def print_channel_statistics(
    channels: list[dict[str, Any]],
    messages_by_channel: dict[str, list[dict[str, Any]]],
) -> None:
    """Print statistics about channels and messages.

    Args:
        channels: List of channel objects
        messages_by_channel: Dict mapping channel_id -> messages
    """
    total_messages = sum(len(msgs) for msgs in messages_by_channel.values())

    # Group by channel type
    public_channels = [c for c in channels if not c.get("is_private")]
    private_channels = [c for c in channels if c.get("is_private")]

    # Top channels by message count
    channel_msg_counts = [(c, len(messages_by_channel.get(c["id"], []))) for c in channels]
    channel_msg_counts.sort(key=lambda x: x[1], reverse=True)

    top_channels = ", ".join(
        f"#{c.get('name', c['id'])}={count}" for c, count in channel_msg_counts[:10]
    )

    logger.info(
        "Slack channel statistics: %d channels (%d public, %d private), %d total messages. Top: %s",
        len(channels),
        len(public_channels),
        len(private_channels),
        total_messages,
        top_channels,
    )
