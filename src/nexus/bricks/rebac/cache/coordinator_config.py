"""Config dataclasses for CacheCoordinator constructor (Issue #3396).

Groups related parameters into semantic units to reduce constructor
parameter count from 15+ to ~6-7 top-level arguments.

Related: Issue #3396, Issue #1459
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream
    from nexus.bricks.rebac.cache.invalidation_stream import InvalidationStream
    from nexus.bricks.rebac.cache.pubsub_invalidation import PubSubInvalidation
    from nexus.bricks.rebac.cache.read_fence import ReadFence


@dataclass(frozen=True, slots=True)
class InvalidationChannels:
    """All cross-process / cross-zone invalidation channels.

    Groups DT_STREAM (intra-zone), Pub/Sub (cross-zone hints), and
    DurableStream (cross-zone guaranteed delivery) into one config.
    """

    stream: InvalidationStream | None = None
    """Intra-zone ordered invalidation stream (DT_STREAM)."""

    pubsub: PubSubInvalidation | None = None
    """Cross-zone fire-and-forget invalidation hints."""

    durable_stream: DurableInvalidationStream | None = None
    """Cross-zone durable invalidation via Redis Streams (Issue #3396)."""

    read_fence: ReadFence | None = None
    """Per-zone watermark for read-path staleness detection."""


@dataclass(frozen=True, slots=True)
class DatabaseCallbacks:
    """Database access callbacks for L2 cache invalidation.

    Used by eager recompute and deep invalidation (Issue #2179 Step 2.5).
    """

    connection_factory: Callable[..., Any] | None = None
    """Context manager for database connections."""

    get_connection: Callable[[], Any] | None = None
    """Get a raw DBAPI connection."""

    close_connection: Callable[[Any], None] | None = None
    """Close a raw DBAPI connection."""

    create_cursor: Callable[[Any], Any] | None = None
    """Create a cursor from a connection."""

    fix_sql: Callable[[str], str] | None = None
    """Adapt SQL placeholders for the DB dialect."""


@dataclass(frozen=True, slots=True)
class RecomputeCallbacks:
    """Eager cache recomputation callbacks (PR #969).

    For simple direct relations, recompute the permission check
    result instead of just invalidating the cache.
    """

    get_namespace: Callable[[str], Any] | None = None
    """Lookup namespace config by object type."""

    compute_permission: Callable[..., bool] | None = None
    """Compute a permission check result."""

    cache_check_result: Callable[..., None] | None = None
    """Store a check result in the L1 cache."""


@dataclass(slots=True)
class CoordinatorConfig:
    """Top-level configuration for CacheCoordinator.

    Bundles all sub-configs and flags into a single init object.
    Mutable (not frozen) so tests can easily override individual fields.
    """

    channels: InvalidationChannels = field(default_factory=InvalidationChannels)
    database: DatabaseCallbacks = field(default_factory=DatabaseCallbacks)
    recompute: RecomputeCallbacks = field(default_factory=RecomputeCallbacks)

    enable_async_recompute: bool = True
    """Disable for SQLite/StaticPool where background threads share DBAPI connection."""

    cache_ttl_seconds: int = 300
    """L2 cache TTL for stats reporting."""

    get_tuple_version: Callable[[], int] | None = None
    set_tuple_version: Callable[[int], None] | None = None
