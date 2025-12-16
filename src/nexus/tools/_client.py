"""Shared client utilities for Nexus tools.

This module provides common helper functions for creating authenticated
Nexus clients from LangGraph configuration.
"""

from typing import Any

from langchain_core.runnables import RunnableConfig

from nexus.remote import RemoteNexusFS


def _get_nexus_client(config: RunnableConfig, state: dict[str, Any] | None = None) -> RemoteNexusFS:
    """Create authenticated RemoteNexusFS from config or state.

    Requires authentication via metadata.x_auth: "Bearer <token>" or state["context"]["x_auth"]

    Args:
        config: Runtime configuration (provided by framework) containing auth metadata
        state: Optional agent state that may contain context with x_auth

    Returns:
        Authenticated RemoteNexusFS instance

    Raises:
        ValueError: If x_auth is missing or invalid
    """
    # Get API key from metadata.x_auth or state.context
    metadata = config.get("metadata", {})
    x_auth = metadata.get("x_auth", "")
    server_url = metadata.get("nexus_server_url", "")

    # Fallback to state context if metadata is empty
    if not x_auth and state:
        context = state.get("context", {})
        x_auth = context.get("x_auth", "")
        server_url = server_url or context.get("nexus_server_url", "")

    if not x_auth:
        raise ValueError(
            "Missing x_auth in metadata. "
            "Frontend must pass API key via metadata: {'x_auth': 'Bearer <token>'}"
        )

    # Strip "Bearer " prefix if present
    api_key = x_auth.removeprefix("Bearer ").strip()

    if not api_key:
        raise ValueError("Invalid x_auth format. Expected 'Bearer <token>', got: " + x_auth)

    return RemoteNexusFS(server_url=server_url, api_key=api_key)
