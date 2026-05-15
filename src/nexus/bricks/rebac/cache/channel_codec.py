"""Shared channel key encoder/decoder for invalidation channels.

Provides a consistent, safe encoding for channel names used by both
PubSub (fire-and-forget) and DurableInvalidationStream (Redis Streams).

Uses pipe '|' as delimiter instead of ':' to avoid ambiguity when
zone_id contains colons (e.g. 'us-east-1:partition-2').

Related: Issue #3396
"""

from __future__ import annotations

# Delimiter chosen to avoid conflict with zone_id colons and Redis key conventions.
_DELIM = "|"


def encode_channel(prefix: str, zone_id: str, layer: str) -> str:
    """Encode a channel name from components.

    Args:
        prefix: Channel prefix (e.g. 'rebac:invalidation' or 'rebac:durable')
        zone_id: Zone identifier (may contain colons)
        layer: Cache layer name (e.g. 'boundary', 'all')

    Returns:
        Encoded channel string safe for Redis key usage.
    """
    return f"{prefix}{_DELIM}{zone_id}{_DELIM}{layer}"


def decode_channel(channel: str) -> tuple[str, str, str] | None:
    """Decode a channel name into (prefix, zone_id, layer).

    Returns:
        Tuple of (prefix, zone_id, layer) or None if the channel
        doesn't match the expected format.
    """
    parts = channel.split(_DELIM)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]
