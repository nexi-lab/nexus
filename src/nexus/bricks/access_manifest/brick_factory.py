"""Auto-discoverable brick factory for AccessManifestService (Issue #1754)."""

from __future__ import annotations

from typing import Any

BRICK_NAME: str | None = None  # No deployment profile gate (always enabled)
TIER = "independent"
RESULT_KEY = "access_manifest_service"


def create(ctx: Any, _system: dict[str, Any]) -> Any:
    """Create AccessManifestService. Lazy imports inside."""
    rebac = _system.get("rebac_manager")
    if ctx.record_store is None or rebac is None:
        return None
    from nexus.bricks.access_manifest.service import AccessManifestService

    return AccessManifestService(record_store=ctx.record_store, rebac_manager=rebac)
