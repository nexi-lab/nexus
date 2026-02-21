"""Auto-discoverable brick factory for Identity brick (Issue #2436).

Provides ``KeyService`` for cryptographic agent identity management.
"""

from __future__ import annotations

from typing import Any

BRICK_NAME: str | None = None  # No deployment profile gate (always enabled)
TIER = "independent"
RESULT_KEY = "identity_service"


def create(ctx: Any, _system: dict[str, Any]) -> Any:
    """Create KeyService with record_store + IdentityCrypto. Lazy imports."""
    if ctx.record_store is None:
        return None
    from nexus.bricks.identity.crypto import IdentityCrypto
    from nexus.bricks.identity.key_service import KeyService

    return KeyService(
        record_store=ctx.record_store,
        crypto=IdentityCrypto(),
    )
