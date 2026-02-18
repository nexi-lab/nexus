"""Shared token parsing utilities for the Nexus server.

Provides a single source of truth for parsing sk-style API tokens
used by both authentication and rate limiting.
"""

from __future__ import annotations

from typing import NamedTuple


class SKTokenFields(NamedTuple):
    """Parsed fields from an sk-<zone>_<user>_<id>_<random> token."""

    zone: str | None
    user: str | None
    key_id: str | None


def parse_sk_token(token: str) -> SKTokenFields | None:
    """Parse an sk-style API token into its constituent fields.

    Token format: sk-<zone>_<user>_<id>_<random-hex>

    Args:
        token: The raw token string (must start with "sk-").

    Returns:
        SKTokenFields with zone/user/key_id, or None if not a valid sk- token.
        Empty string parts are returned as None.
    """
    if not token.startswith("sk-"):
        return None
    remainder = token[3:]
    parts = remainder.split("_")
    if len(parts) < 2:
        return None
    return SKTokenFields(
        zone=parts[0] or None,
        user=parts[1] or None,
        key_id=parts[2] if len(parts) >= 3 and parts[2] else None,
    )
