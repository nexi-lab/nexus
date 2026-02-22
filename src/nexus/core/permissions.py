"""Permission types — re-exports from contracts layer.

These types were moved to ``nexus.contracts.types`` as part of the
four-tier architecture refactor. This module provides backward
compatibility for existing importers.
"""

from nexus.contracts.types import OperationContext, Permission

__all__ = ["OperationContext", "Permission"]


def check_stale_session(agent_registry: object, context: OperationContext) -> None:
    """Re-export check_stale_session from contracts.agent_utils."""
    from nexus.contracts.agent_utils import check_stale_session as _check

    _check(agent_registry, context)
