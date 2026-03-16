"""Auto-discoverable brick factory for DelegationService (Issue #2180)."""

from typing import Any

BRICK_NAME: str | None = None  # No deployment profile gate (always enabled)
TIER = "independent"
RESULT_KEY = "delegation_service"


def create(ctx: Any, system: dict[str, Any]) -> Any:
    """Create DelegationService. Lazy imports inside."""
    if ctx.record_store is None:
        return None
    from nexus.bricks.delegation.service import DelegationService

    return DelegationService(
        record_store=ctx.record_store,
        rebac_manager=system["rebac_manager"],
        entity_registry=system.get("entity_registry"),
    )
