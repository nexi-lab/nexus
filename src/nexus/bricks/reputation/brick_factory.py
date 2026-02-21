"""Auto-discoverable brick factory for ReputationService (Issue #2180)."""

from typing import Any

BRICK_NAME: str | None = None  # No deployment profile gate (always enabled)
TIER = "independent"
RESULT_KEY = "reputation_service"


def create(ctx: Any, _system: dict[str, Any]) -> Any:
    """Create ReputationService. Lazy imports inside."""
    if ctx.record_store is None:
        return None
    from nexus.bricks.reputation.reputation_service import ReputationService

    return ReputationService(record_store=ctx.record_store)
