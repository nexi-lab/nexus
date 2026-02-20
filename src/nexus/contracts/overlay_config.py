"""Overlay workspace configuration type (shared across tiers).

OverlayConfig is a pure data class with no service dependencies,
so it belongs in contracts/ rather than services/.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class OverlayConfig:
    """Configuration for an overlay workspace.

    Attributes:
        enabled: Whether overlay resolution is active
        base_manifest_hash: CAS hash of the base workspace snapshot manifest
        workspace_path: Root path of the workspace (e.g., "/my-workspace")
        agent_id: Agent ID owning this overlay (for multi-agent isolation)
    """

    enabled: bool = False
    base_manifest_hash: str | None = None
    workspace_path: str = ""
    agent_id: str | None = None
