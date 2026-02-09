"""Google A2A (Agent-to-Agent) protocol endpoint for Nexus.

This module implements the A2A protocol specification, enabling Nexus to
participate in the agent interoperability ecosystem as one of three protocol
surfaces (alongside VFS and MCP).

See: https://a2a-protocol.org/latest/specification/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter


def create_a2a_router(
    *,
    nexus_fs: Any = None,
    config: Any = None,
    base_url: str | None = None,
) -> APIRouter:
    """Create the A2A protocol FastAPI router.

    Args:
        nexus_fs: NexusFS instance for backend operations.
        config: NexusConfig instance for Agent Card generation.
        base_url: Base URL for the Agent Card endpoint URL field.
            If None, defaults to "http://localhost:2026".

    Returns:
        Configured FastAPI APIRouter with A2A endpoints.
    """
    from nexus.a2a.router import build_router

    return build_router(_nexus_fs=nexus_fs, config=config, base_url=base_url)
