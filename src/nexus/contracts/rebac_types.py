"""Tier-neutral ReBAC types for cross-brick use (Issue #2190).

Canonical home for shared ReBAC vocabulary types used across the permissions
system service, memory brick, and bulk permission checker.

This module has **zero** runtime imports from ``nexus.*`` --- only stdlib ---
so bricks, services, and backends can depend on it without pulling in the
ReBAC brick.

Backward-compat shims:
    - ``nexus.bricks.rebac.types`` re-exports graph types
    - ``nexus.bricks.rebac.domain`` re-exports Entity, WILDCARD_SUBJECT
    - ``nexus.bricks.rebac.cross_zone`` re-exports CROSS_ZONE_ALLOWED_RELATIONS
"""

from dataclasses import dataclass
from typing import Any

__all__ = [
    "WriteResult",
    "CheckResult",
    "TraversalStats",
    "GraphLimits",
    "GraphLimitExceeded",
    # Entity (from rebac.domain)
    "Entity",
    "WILDCARD_SUBJECT",
    # Cross-zone (from rebac.cross_zone)
    "CROSS_ZONE_ALLOWED_RELATIONS",
]


@dataclass(slots=True)
class WriteResult:
    """Result of a permission write with consistency metadata (Issue #1081).

    Following the Zanzibar zookie pattern, writes return a consistency token
    that can be used for subsequent read-your-writes queries.

    Attributes:
        tuple_id: UUID of the created relationship tuple
        revision: The revision number after this write (for AT_LEAST_AS_FRESH)
        consistency_token: Opaque token encoding the revision (for clients)
        written_at_ms: Timestamp when write was persisted (epoch ms)
    """

    tuple_id: str
    revision: int
    consistency_token: str
    written_at_ms: float


@dataclass(slots=True)
class TraversalStats:
    """Statistics from graph traversal (P0-5).

    Used for monitoring and debugging graph limits.
    """

    queries: int = 0
    nodes_visited: int = 0
    max_depth_reached: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    duration_ms: float = 0.0


@dataclass(slots=True)
class CheckResult:
    """Result of a permission check with consistency metadata.

    Attributes:
        allowed: Whether permission is granted
        consistency_token: Version token for this check (monotonic counter)
        decision_time_ms: Time taken to compute decision
        cached: Whether result came from cache
        cache_age_ms: Age of cached result (None if not cached)
        traversal_stats: Graph traversal statistics
        indeterminate: Whether decision was indeterminate (denied due to limits, not policy)
        limit_exceeded: The limit that was exceeded (if indeterminate=True)
    """

    allowed: bool
    consistency_token: str
    decision_time_ms: float
    cached: bool
    cache_age_ms: float | None = None
    traversal_stats: TraversalStats | None = None
    indeterminate: bool = False  # BUGFIX (Issue #5): Track limit-driven denials
    limit_exceeded: "GraphLimitExceeded | None" = None  # BUGFIX (Issue #5): Which limit was hit


# ============================================================================
# Graph Limits and DoS Protection (from rebac.types)
# ============================================================================


class GraphLimits:
    """Hard limits for graph traversal to prevent DoS attacks.

    These limits ensure permission checks complete within bounded time
    and memory, even with pathological graphs.
    """

    MAX_DEPTH = 50  # Max recursion depth (increased for deep directory hierarchies)
    MAX_FAN_OUT = 1000  # Max edges per union/expand
    MAX_EXECUTION_TIME_MS = 1000  # 1 second timeout for permission computation
    MAX_VISITED_NODES = 10000  # Memory bound
    MAX_TUPLE_QUERIES = 100  # DB query limit


class GraphLimitExceeded(Exception):
    """Raised when graph traversal exceeds limits.

    Attributes:
        limit_type: Type of limit exceeded (depth, fan_out, timeout, nodes, queries)
        limit_value: Configured limit value
        actual_value: Actual value when limit was hit
        path: Partial proof path before limit
    """

    def __init__(
        self,
        limit_type: str,
        limit_value: int | float,
        actual_value: int | float,
        path: list[str] | None = None,
    ):
        self.limit_type = limit_type
        self.limit_value = limit_value
        self.actual_value = actual_value
        self.path = path or []
        super().__init__(f"Graph {limit_type} limit exceeded: {actual_value} > {limit_value}")

    def to_http_error(self) -> dict[str, Any]:
        """Convert to HTTP error response."""
        if self.limit_type == "timeout":
            return {
                "code": 503,
                "message": "Permission check timeout",
                "limit": self.limit_value,
                "actual": self.actual_value,
            }
        else:
            return {
                "code": 429,
                "message": f"Graph {self.limit_type} limit exceeded",
                "limit": self.limit_value,
                "actual": self.actual_value,
            }


# ============================================================================
# Entity (from rebac.domain)
# ============================================================================


@dataclass(slots=True)
class Entity:
    """Represents an entity in the ReBAC system.

    Attributes:
        entity_type: Type of entity (agent, group, file, etc.)
        entity_id: Unique identifier for the entity
    """

    entity_type: str
    entity_id: str

    def __post_init__(self) -> None:
        """Validate entity."""
        if not self.entity_type:
            raise ValueError("entity_type is required")
        if not self.entity_id:
            raise ValueError("entity_id is required")

    def to_tuple(self) -> tuple[str, str]:
        """Convert to (type, id) tuple."""
        return (self.entity_type, self.entity_id)

    @classmethod
    def from_tuple(cls, tup: tuple[str, str]) -> "Entity":
        """Create entity from (type, id) tuple."""
        return cls(entity_type=tup[0], entity_id=tup[1])

    def __str__(self) -> str:
        return f"{self.entity_type}:{self.entity_id}"


# Wildcard subject for public access
WILDCARD_SUBJECT: tuple[str, str] = ("*", "*")

# ============================================================================
# Cross-zone (from rebac.cross_zone)
# ============================================================================

# Relations that are allowed to cross zone boundaries.
# These relations can link subjects and objects from different zones.
CROSS_ZONE_ALLOWED_RELATIONS: frozenset[str] = frozenset(
    {
        "shared-viewer",  # Read access via cross-zone share
        "shared-editor",  # Read + Write access via cross-zone share
        "shared-owner",  # Full access via cross-zone share
    }
)
