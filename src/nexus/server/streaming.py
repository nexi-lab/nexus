"""Stream token signing and verification for local backend streaming URLs.

This module provides HMAC-SHA256 based token generation and verification
for secure, time-limited file streaming access via the local backend.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time

# Secret key for signing stream tokens (persistent across restarts if set via env)
_STREAM_SECRET: bytes | None = None


def _get_stream_secret() -> bytes:
    """Get or generate the stream token signing secret."""
    global _STREAM_SECRET
    if _STREAM_SECRET is None:
        env_secret = os.environ.get("NEXUS_STREAM_SECRET")
        # Use env var if set, otherwise generate random secret (changes on restart)
        _STREAM_SECRET = env_secret.encode() if env_secret else secrets.token_bytes(32)
    return _STREAM_SECRET


def _sign_stream_token(path: str, expires_in: int, zone_id: str = "default") -> str:
    """Generate a signed token for streaming access to a file.

    Token format: {expires_at}.{signature}
    Where signature = HMAC-SHA256(path:expires_at:zone_id)[:16]

    Args:
        path: Virtual file path
        expires_in: Token validity in seconds
        zone_id: Zone ID for isolation

    Returns:
        Signed token string
    """
    expires_at = int(time.time()) + expires_in
    payload = f"{path}:{expires_at}:{zone_id}"
    signature = hmac.new(_get_stream_secret(), payload.encode(), "sha256").hexdigest()[:16]
    return f"{expires_at}.{signature}"


def _verify_stream_token(token: str, path: str, zone_id: str = "default") -> bool:
    """Verify a stream token is valid and not expired.

    Args:
        token: Token string from _sign_stream_token
        path: Virtual file path (must match token)
        zone_id: Zone ID (must match token)

    Returns:
        True if token is valid, False otherwise
    """
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return False

        expires_at_str, signature = parts
        expires_at = int(expires_at_str)

        # Check expiration
        if expires_at < time.time():
            return False

        # Verify signature
        payload = f"{path}:{expires_at}:{zone_id}"
        expected_sig = hmac.new(_get_stream_secret(), payload.encode(), "sha256").hexdigest()[:16]

        return hmac.compare_digest(signature, expected_sig)
    except (ValueError, TypeError):
        return False


def _reset_stream_secret() -> None:
    """Reset the stream secret. Used by tests for isolation."""
    global _STREAM_SECRET
    _STREAM_SECRET = None
