"""Session routing utilities.

Derives session keys from channel-specific identifiers.
Based on clawdbot's session key patterns.
"""

from __future__ import annotations


def normalize_token(value: str | None) -> str:
    """Normalize a token for use in session keys.

    Args:
        value: Raw token value

    Returns:
        Lowercase, trimmed token
    """
    if not value:
        return ""
    return value.strip().lower()


def derive_session_key(
    channel: str,
    account_id: str,
    chat_id: str,
) -> str:
    """Derive a session key from channel identifiers.

    Creates an explicit, human-readable session key without hashing.
    All conversations are treated as "boardrooms" - same model for DMs and groups.

    Args:
        channel: Platform name (e.g., "discord", "slack", "telegram")
        account_id: Account/workspace/guild ID (required)
        chat_id: Channel/conversation/chat ID (the "boardroom")

    Returns:
        Session key in format: {channel}:{account_id}:{chat_id}

    Examples:
        >>> derive_session_key("discord", "guild_123", "channel_456")
        'discord:guild_123:channel_456'

        >>> derive_session_key("slack", "T0123ABC", "C456DEF")
        'slack:t0123abc:c456def'

        >>> derive_session_key("telegram", "bot_42", "chat_99")
        'telegram:bot_42:chat_99'
    """
    # Validate required inputs
    if not channel:
        raise ValueError("channel is required")
    if not account_id:
        raise ValueError("account_id is required")
    if not chat_id:
        raise ValueError("chat_id is required")

    # Normalize components
    channel_norm = normalize_token(channel)
    account_norm = normalize_token(account_id)
    chat_norm = normalize_token(chat_id)

    if not channel_norm:
        raise ValueError("channel cannot be empty after normalization")
    if not account_norm:
        raise ValueError("account_id cannot be empty after normalization")
    if not chat_norm:
        raise ValueError("chat_id cannot be empty after normalization")

    # Validate no colons in parts
    for part in [channel_norm, account_norm, chat_norm]:
        if ":" in part:
            raise ValueError(f"Colon not allowed in session key component: {part}")

    return f"{channel_norm}:{account_norm}:{chat_norm}"


def parse_session_key(session_key: str) -> dict[str, str]:
    """Parse a session key back into its components.

    Args:
        session_key: Session key string

    Returns:
        Dict with keys: channel, account_id, chat_id

    Raises:
        ValueError: If session key format is invalid
    """
    parts = session_key.split(":")

    if len(parts) != 3:
        raise ValueError(
            f"Invalid session key format: {session_key}. "
            "Expected format: channel:account_id:chat_id"
        )

    return {
        "channel": parts[0],
        "account_id": parts[1],
        "chat_id": parts[2],
    }
