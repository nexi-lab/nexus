"""Shared type definitions for the ReBAC brick (Issue #1385).

This module holds enums, dataclasses, and exception types used across the
ReBAC package. Keeping them in a dedicated module avoids circular imports
and clarifies the public API surface.

Canonical location: ``nexus.rebac.types``
Backward-compat shim: ``nexus.services.permissions.types``
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "ConsistencyLevel",
    "ConsistencyMode",
    "ConsistencyRequirement",
    "WriteResult",
    "CheckResult",
    "TraversalStats",
    "GraphLimits",
    "GraphLimitExceeded",
]


# ============================================================================
# P0-1: Consistency Levels and Version Tokens
# ============================================================================


class ConsistencyLevel(Enum):
    """Consistency levels for permission checks.

    Controls cache behavior and staleness guarantees:
    - EVENTUAL: Use cache (up to 5min staleness), fastest
    - BOUNDED: Max 1s staleness
    - STRONG: Bypass cache, fresh read, slowest but most accurate

    Note: For new code, prefer ConsistencyMode with ConsistencyRequirement
    which aligns with SpiceDB/Zanzibar naming conventions.
    """

    EVENTUAL = "eventual"  # Use cache (5min staleness)
    BOUNDED = "bounded"  # Max 1s staleness
    STRONG = "strong"  # Bypass cache, fresh read


class ConsistencyMode(Enum):
    """Per-request consistency modes aligned with SpiceDB/Zanzibar (Issue #1081).

    Provides fine-grained control over cache behavior on a per-request basis,
    following industry best practices from Google Zanzibar and SpiceDB.

    Modes:
    - MINIMIZE_LATENCY: Use cache for fastest response (~99% of requests)
    - AT_LEAST_AS_FRESH: Cache must be >= min_revision (read-after-write)
    - FULLY_CONSISTENT: Bypass cache entirely (security audits, <1% of requests)

    See:
    - https://authzed.com/docs/spicedb/concepts/consistency
    - https://www.usenix.org/system/files/atc19-pang.pdf (Zanzibar paper)
    """

    MINIMIZE_LATENCY = "minimize_latency"  # Default: use cache, fastest
    AT_LEAST_AS_FRESH = "at_least_as_fresh"  # Cache if revision >= min_revision
    FULLY_CONSISTENT = "fully_consistent"  # Bypass cache, slowest but freshest


@dataclass(slots=True)
class ConsistencyRequirement:
    """Per-request consistency requirement (Issue #1081).

    Combines a consistency mode with optional parameters like min_revision.
    This follows the SpiceDB/Zanzibar pattern for per-request consistency control.

    Examples:
        # Default: maximize cache usage (99% of requests)
        ConsistencyRequirement()

        # Read-after-write: ensure we see a recent write
        ConsistencyRequirement(
            mode=ConsistencyMode.AT_LEAST_AS_FRESH,
            min_revision=write_result.revision
        )

        # Security audit: bypass all caches
        ConsistencyRequirement(mode=ConsistencyMode.FULLY_CONSISTENT)

    Attributes:
        mode: The consistency mode to use
        min_revision: Required for AT_LEAST_AS_FRESH - minimum acceptable revision
    """

    mode: ConsistencyMode = ConsistencyMode.MINIMIZE_LATENCY
    min_revision: int | None = None

    def __post_init__(self) -> None:
        """Validate consistency requirement."""
        if self.mode == ConsistencyMode.AT_LEAST_AS_FRESH and self.min_revision is None:
            raise ValueError(
                "min_revision is required for AT_LEAST_AS_FRESH mode. "
                "Pass the revision from a previous write operation."
            )

    def to_legacy_level(self) -> ConsistencyLevel:
        """Convert to legacy ConsistencyLevel for backward compatibility."""
        if self.mode == ConsistencyMode.FULLY_CONSISTENT:
            return ConsistencyLevel.STRONG
        elif self.mode == ConsistencyMode.AT_LEAST_AS_FRESH:
            return ConsistencyLevel.BOUNDED
        else:
            return ConsistencyLevel.EVENTUAL


@dataclass(slots=True)
class WriteResult:
    """Result of a permission write with consistency metadata (Issue #1081).

    Following the Zanzibar zookie pattern, writes return a consistency token
    that can be used for subsequent read-your-writes queries.

    Example:
        # Write a permission
        result = manager.rebac_write(subject, relation, object, zone_id=zone)

        # Immediately check with read-your-writes guarantee
        allowed = manager.rebac_check(
            subject, permission, object,
            consistency=ConsistencyRequirement(
                mode=ConsistencyMode.AT_LEAST_AS_FRESH,
                min_revision=result.revision
            )
        )

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
    limit_exceeded: GraphLimitExceeded | None = None  # BUGFIX (Issue #5): Which limit was hit


# ============================================================================
# P0-5: Graph Limits and DoS Protection
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
