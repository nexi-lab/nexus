"""Auto-discoverable brick factory for Auth brick (Issue #2436).

Provides the ``api_key_creator`` class reference used by kernel-tier
services (AgentService, UserProvisioning) to create API keys.
"""

from __future__ import annotations

from typing import Any

BRICK_NAME: str | None = None  # No deployment profile gate (always enabled)
TIER = "independent"
RESULT_KEY = "api_key_creator"


def create(_ctx: Any, _system: dict[str, Any]) -> Any:
    """Return DatabaseAPIKeyAuth class (not instance). Lazy import."""
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

    return DatabaseAPIKeyAuth
