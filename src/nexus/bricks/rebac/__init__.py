"""ReBAC brick — Relationship-Based Access Control (Issue #1385, #2179).

Provides Zanzibar-style ReBAC as a removable Brick (tier 4):
- Core ReBACManager (facade delegating to focused sub-components)
- Multi-layer caching (Tiger, Leopard, Boundary, L1)
- Graph traversal with DoS protection (P0-5)
- Zone isolation and consistency guarantees (P0-1, P0-2)
- Permission enforcement with admin/system bypass (P0-4)

Zero imports from ``nexus.core`` at runtime (LEGO Principle 3).
All kernel types come from ``nexus.contracts.*``.

Usage:
    from nexus.bricks.rebac import ReBACManager, PermissionEnforcer, CheckResult
"""

import importlib
from typing import TYPE_CHECKING

from nexus.contracts.rebac_types import (
    WILDCARD_SUBJECT,
    CheckResult,
    Entity,
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
    WriteResult,
)

if TYPE_CHECKING:
    from nexus.bricks.rebac.enforcer import PermissionEnforcer
    from nexus.bricks.rebac.manager import ReBACManager

# Lazy imports for heavy modules (follow skills/pay pattern)
_LAZY_IMPORTS: dict[str, str] = {
    "ReBACManager": "nexus.bricks.rebac.manager",
    "PermissionEnforcer": "nexus.bricks.rebac.enforcer",
    "EntityRegistry": "nexus.bricks.rebac.entity_registry",
    "NamespaceManager": "nexus.bricks.rebac.namespace_manager",
    "AsyncReBACManager": "nexus.bricks.rebac.async_manager",
    "AsyncPermissionEnforcer": "nexus.bricks.rebac.async_permissions",
    "MemoryPermissionEnforcer": "nexus.bricks.rebac.memory_permission_enforcer",
    "AsyncCircuitBreaker": "nexus.bricks.rebac.circuit_breaker",
    "CircuitBreakerConfig": "nexus.bricks.rebac.circuit_breaker",
}

__all__ = [
    # Types (eagerly loaded from contracts)
    "CheckResult",
    "Entity",
    "GraphLimitExceeded",
    "GraphLimits",
    "TraversalStats",
    "WILDCARD_SUBJECT",
    "WriteResult",
    # Classes (lazily loaded)
    "ReBACManager",
    "PermissionEnforcer",
    "EntityRegistry",
    "NamespaceManager",
    "AsyncReBACManager",
    "AsyncPermissionEnforcer",
    "MemoryPermissionEnforcer",
    "AsyncCircuitBreaker",
    "CircuitBreakerConfig",
]


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        module = importlib.import_module(_LAZY_IMPORTS[name])
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
