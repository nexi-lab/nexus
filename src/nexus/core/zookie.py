"""
Zookie Consistency Tokens for Filesystem Operations (Issue #1187)

This module implements Zanzibar-style consistency tokens ("zookies") for file operations,
enabling read-after-write consistency guarantees. Following the SpiceDB/Zanzibar pattern,
zookies are opaque tokens that encode a point-in-time snapshot of the filesystem state.

Usage:
    from nexus.core.zookie import Zookie, InvalidZookieError, ConsistencyTimeoutError

    # On write operations, encode and return a zookie
    revision = increment_revision(zone_id)
    token = Zookie.encode(zone_id, revision)
    return {"etag": etag, "zookie": token}

    # On read operations, decode and validate the zookie
    try:
        zookie = Zookie.decode(token)
        if not wait_for_revision(zookie.zone_id, zookie.revision):
            raise ConsistencyTimeoutError(...)
    except InvalidZookieError as e:
        # Handle invalid token

See:
    - https://authzed.com/docs/spicedb/concepts/consistency
    - https://research.google/pubs/pub48190/ (Zanzibar paper)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

# Secret key for HMAC checksum (not for security, just tamper detection)
# In production, this could be configurable via environment variable
_ZOOKIE_HMAC_KEY = b"nexus-zookie-v1-checksum-key"

# Token version prefix for future format evolution
_ZOOKIE_VERSION = "nz1"


class InvalidZookieError(Exception):
    """Raised when a zookie token cannot be decoded or validated."""

    def __init__(self, message: str, token: str | None = None):
        super().__init__(message)
        self.token = token
        self.message = message


class ConsistencyTimeoutError(Exception):
    """Raised when waiting for a revision times out.

    This indicates the system could not achieve the requested consistency
    level within the timeout period.
    """

    def __init__(
        self,
        message: str,
        zone_id: str,
        requested_revision: int,
        current_revision: int,
        timeout_ms: float,
    ):
        super().__init__(message)
        self.zone_id = zone_id
        self.requested_revision = requested_revision
        self.current_revision = current_revision
        self.timeout_ms = timeout_ms


@dataclass(slots=True, frozen=True)
class Zookie:
    """Filesystem consistency token (Zanzibar zookie pattern).

    A zookie encodes a point-in-time snapshot of the filesystem state for a zone.
    It can be used to ensure read-after-write consistency by passing the zookie
    from a write operation to subsequent read operations.

    Attributes:
        zone_id: The zone this zookie applies to
        revision: The monotonic revision number at the time of the write
        created_at_ms: Epoch milliseconds when the zookie was created (for debugging)

    Token Format:
        nz1.{base64(zone_id)}.{revision}.{checksum}

        - nz1: Version prefix (Nexus Zookie v1)
        - base64(zone_id): URL-safe base64 encoded zone ID
        - revision: Monotonic revision number
        - checksum: First 8 chars of HMAC-SHA256 for tamper detection

    Example:
        >>> token = Zookie.encode("zone_123", 42)
        >>> print(token)
        'nz1.dGVuYW50XzEyMw.42.a1b2c3d4'

        >>> zookie = Zookie.decode(token)
        >>> print(zookie.zone_id, zookie.revision)
        'zone_123' 42
    """

    zone_id: str
    revision: int
    created_at_ms: float

    @classmethod
    def encode(cls, zone_id: str, revision: int) -> str:
        """Encode a zookie to an opaque string token.

        Args:
            zone_id: The zone ID to encode
            revision: The revision number to encode

        Returns:
            An opaque string token that can be passed to clients

        Example:
            >>> token = Zookie.encode("org_123", 456)
            >>> token.startswith("nz1.")
            True
        """
        created_at_ms = time.time() * 1000

        # Base64 encode zone_id for URL safety
        zone_b64 = base64.urlsafe_b64encode(zone_id.encode()).decode().rstrip("=")

        # Create the payload (without checksum)
        payload = f"{_ZOOKIE_VERSION}.{zone_b64}.{revision}.{int(created_at_ms)}"

        # Compute HMAC checksum (first 8 chars)
        checksum = _compute_checksum(payload)

        return f"{payload}.{checksum}"

    @classmethod
    def decode(cls, token: str) -> Zookie:
        """Decode a string token to a Zookie object.

        Args:
            token: The opaque string token from a previous encode() call

        Returns:
            A Zookie object with the decoded values

        Raises:
            InvalidZookieError: If the token is malformed, has wrong version,
                or fails checksum validation
        """
        if not token or not isinstance(token, str):
            raise InvalidZookieError("Token must be a non-empty string", token)

        parts = token.split(".")
        if len(parts) != 5:
            raise InvalidZookieError(
                f"Invalid token format: expected 5 parts, got {len(parts)}", token
            )

        version, zone_b64, revision_str, created_at_str, checksum = parts

        # Validate version
        if version != _ZOOKIE_VERSION:
            raise InvalidZookieError(
                f"Unsupported zookie version: {version} (expected {_ZOOKIE_VERSION})",
                token,
            )

        # Validate checksum
        payload = f"{version}.{zone_b64}.{revision_str}.{created_at_str}"
        expected_checksum = _compute_checksum(payload)
        if not hmac.compare_digest(checksum, expected_checksum):
            raise InvalidZookieError("Invalid zookie checksum (token may be corrupted)", token)

        # Decode zone_id (add padding if needed)
        try:
            padding = 4 - (len(zone_b64) % 4)
            if padding != 4:
                zone_b64 += "=" * padding
            zone_id = base64.urlsafe_b64decode(zone_b64).decode()
        except Exception as e:
            raise InvalidZookieError(f"Invalid zone encoding: {e}", token) from e

        # Parse revision
        try:
            revision = int(revision_str)
            if revision < 0:
                raise ValueError("Revision must be non-negative")
        except ValueError as e:
            raise InvalidZookieError(f"Invalid revision: {e}", token) from e

        # Parse created_at
        try:
            created_at_ms = float(created_at_str)
        except ValueError as e:
            raise InvalidZookieError(f"Invalid timestamp: {e}", token) from e

        return cls(zone_id=zone_id, revision=revision, created_at_ms=created_at_ms)

    def is_at_least(self, min_revision: int) -> bool:
        """Check if this zookie satisfies a minimum revision requirement.

        Args:
            min_revision: The minimum acceptable revision

        Returns:
            True if this zookie's revision >= min_revision
        """
        return self.revision >= min_revision

    def age_ms(self) -> float:
        """Get the age of this zookie in milliseconds.

        Returns:
            The number of milliseconds since this zookie was created
        """
        return (time.time() * 1000) - self.created_at_ms

    def __str__(self) -> str:
        """Return a human-readable representation (not the encoded token)."""
        return f"Zookie(zone={self.zone_id}, rev={self.revision}, age={self.age_ms():.0f}ms)"


def _compute_checksum(payload: str) -> str:
    """Compute HMAC-SHA256 checksum, returning first 8 hex chars."""
    mac = hmac.new(_ZOOKIE_HMAC_KEY, payload.encode(), hashlib.sha256)
    return mac.hexdigest()[:8]
