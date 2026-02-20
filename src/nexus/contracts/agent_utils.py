"""Pure utility functions for agent context extraction and config creation.

Extracted from AgentRPCService (Issue #2133) to break the
core/ -> services/ import dependency. These are stateless functions
with no service-layer dependencies.
"""

from __future__ import annotations

from typing import Any


def extract_zone_id(context: dict[str, Any] | Any | None) -> str | None:
    """Extract zone_id from an operation context (dict or object)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("zone_id")
    return getattr(context, "zone_id", None)


def extract_user_id(context: dict[str, Any] | Any | None) -> str | None:
    """Extract user_id from an operation context (dict or object)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("user_id")
    return getattr(context, "user_id", None)


def create_agent_config_data(
    agent_id: str,
    name: str,
    user_id: str,
    description: str | None,
    created_at: str | None,
    metadata: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Build agent config data dictionary."""
    config_data: dict[str, Any] = {
        "agent_id": agent_id,
        "name": name,
        "user_id": user_id,
        "description": description,
        "created_at": created_at,
    }
    if metadata:
        config_data["metadata"] = metadata.copy()
    if api_key is not None:
        config_data["api_key"] = api_key
    return config_data
