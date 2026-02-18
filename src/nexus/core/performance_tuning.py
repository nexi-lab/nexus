"""Per-profile performance tuning configuration (Issue #2071).

Migrates scattered hardcoded performance thresholds to deployment profiles.
Each DeploymentProfile maps to a ProfileTuning frozen dataclass composed
of 5 domain-specific tuning slices.

Pattern follows IOProfile (Issue #1413): frozen dataclasses, profile-selected,
wired via DI in factory.py.

Domain configs:
- ConcurrencyTuning: worker counts, thread pool sizes
- NetworkTuning: HTTP timeouts, webhook timeouts
- StorageTuning: write buffer, batch sizes, DB pool
- SearchTuning: grep workers, search concurrency
- CacheTuning: tiger cache workers, batch sizes

Profile hierarchy matches DeploymentProfile:
    embedded ⊂ lite ⊂ full ⊆ cloud
    (minimal)  (conservative)  (balanced)  (aggressive)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.deployment_profile import DeploymentProfile


# ---------------------------------------------------------------------------
# Domain-specific tuning dataclasses (frozen = immutable at runtime)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConcurrencyTuning:
    """Worker counts and thread pool sizing.

    Consumers: factory.py, batch_executor, task_runner, search_service,
    fastapi_server.
    """

    default_workers: int
    """General-purpose worker count for parallel I/O tasks."""

    thread_pool_size: int
    """AnyIO limiter tokens for sync-in-async operations (NexusConfig.thread_pool_size)."""

    max_async_concurrency: int
    """Semaphore limit for concurrent async operations (indexing, search)."""

    task_runner_workers: int
    """AsyncTaskRunner worker count for durable task queue."""


@dataclass(frozen=True)
class NetworkTuning:
    """HTTP and webhook timeout configuration.

    Consumers: batch_executor, subscriptions/manager, a2a/streaming, mcp/mount.
    """

    default_http_timeout: float
    """Default timeout for outbound HTTP requests (seconds)."""

    webhook_timeout: float
    """Timeout for webhook delivery (seconds)."""

    long_operation_timeout: float
    """Timeout for long-running operations like parsing, sandbox setup (seconds)."""


@dataclass(frozen=True)
class StorageTuning:
    """Write buffer, batch sizes, and database pool configuration.

    Consumers: write_buffer, change_log_store, record_store.
    """

    write_buffer_flush_ms: int
    """WriteBuffer flush interval in milliseconds."""

    write_buffer_max_size: int
    """WriteBuffer max events before forced flush."""

    changelog_chunk_size: int
    """Chunk size for change log batch inserts (SQL variable limit safe)."""

    db_pool_size: int
    """SQLAlchemy connection pool size (primary engine)."""

    db_max_overflow: int
    """SQLAlchemy pool max overflow connections."""


@dataclass(frozen=True)
class SearchTuning:
    """Search strategy thresholds and concurrency.

    Consumers: search/strategies, search/daemon, search/semantic,
    search/vector_db, services/search_service.
    """

    grep_parallel_workers: int
    """Thread pool size for parallel grep strategy."""

    list_parallel_workers: int
    """Thread pool size for parallel directory listing."""

    search_max_concurrency: int
    """Semaphore limit for concurrent search/indexing operations."""

    vector_pool_workers: int
    """Thread pool size for sync vector DB operations."""


@dataclass(frozen=True)
class CacheTuning:
    """Tiger bitmap cache and result cache configuration.

    Consumers: services/permissions/cache/tiger, rebac/cache/tiger,
    rebac/cache/result_cache.
    """

    tiger_max_workers: int
    """Thread pool size for tiger L2 dragonfly operations."""

    tiger_batch_size: int
    """Batch size for tiger bitmap warmup operations."""


# ---------------------------------------------------------------------------
# Composite tuning (all 5 domains)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileTuning:
    """Composite performance tuning for a deployment profile.

    Composed of 5 domain-specific slices.  Each brick receives only
    the slice it needs via constructor injection in factory.py.
    """

    concurrency: ConcurrencyTuning
    network: NetworkTuning
    storage: StorageTuning
    search: SearchTuning
    cache: CacheTuning


# ---------------------------------------------------------------------------
# Profile-to-tuning mappings (frozen — immutable at runtime)
# ---------------------------------------------------------------------------

_EMBEDDED_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=1,
        thread_pool_size=10,
        max_async_concurrency=2,
        task_runner_workers=1,
    ),
    network=NetworkTuning(
        default_http_timeout=15.0,
        webhook_timeout=5.0,
        long_operation_timeout=30.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=200,
        write_buffer_max_size=10,
        changelog_chunk_size=50,
        db_pool_size=2,
        db_max_overflow=2,
    ),
    search=SearchTuning(
        grep_parallel_workers=1,
        list_parallel_workers=2,
        search_max_concurrency=2,
        vector_pool_workers=1,
    ),
    cache=CacheTuning(
        tiger_max_workers=1,
        tiger_batch_size=50,
    ),
)

_LITE_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=2,
        thread_pool_size=50,
        max_async_concurrency=5,
        task_runner_workers=2,
    ),
    network=NetworkTuning(
        default_http_timeout=30.0,
        webhook_timeout=10.0,
        long_operation_timeout=60.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=100,
        write_buffer_max_size=50,
        changelog_chunk_size=200,
        db_pool_size=5,
        db_max_overflow=5,
    ),
    search=SearchTuning(
        grep_parallel_workers=2,
        list_parallel_workers=4,
        search_max_concurrency=5,
        vector_pool_workers=1,
    ),
    cache=CacheTuning(
        tiger_max_workers=2,
        tiger_batch_size=50,
    ),
)

_FULL_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=4,
        thread_pool_size=200,
        max_async_concurrency=10,
        task_runner_workers=4,
    ),
    network=NetworkTuning(
        default_http_timeout=30.0,
        webhook_timeout=10.0,
        long_operation_timeout=120.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=100,
        write_buffer_max_size=100,
        changelog_chunk_size=500,
        db_pool_size=10,
        db_max_overflow=20,
    ),
    search=SearchTuning(
        grep_parallel_workers=4,
        list_parallel_workers=10,
        search_max_concurrency=10,
        vector_pool_workers=2,
    ),
    cache=CacheTuning(
        tiger_max_workers=4,
        tiger_batch_size=100,
    ),
)

_CLOUD_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=8,
        thread_pool_size=400,
        max_async_concurrency=20,
        task_runner_workers=8,
    ),
    network=NetworkTuning(
        default_http_timeout=60.0,
        webhook_timeout=15.0,
        long_operation_timeout=300.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=50,
        write_buffer_max_size=500,
        changelog_chunk_size=1000,
        db_pool_size=20,
        db_max_overflow=40,
    ),
    search=SearchTuning(
        grep_parallel_workers=8,
        list_parallel_workers=20,
        search_max_concurrency=20,
        vector_pool_workers=4,
    ),
    cache=CacheTuning(
        tiger_max_workers=8,
        tiger_batch_size=200,
    ),
)


def _get_profile_tuning_map() -> dict[str, ProfileTuning]:
    """Build profile-to-tuning mapping (lazy import to avoid circular)."""
    from nexus.core.deployment_profile import DeploymentProfile

    return {
        DeploymentProfile.EMBEDDED: _EMBEDDED_TUNING,
        DeploymentProfile.LITE: _LITE_TUNING,
        DeploymentProfile.FULL: _FULL_TUNING,
        DeploymentProfile.CLOUD: _CLOUD_TUNING,
    }


def resolve_profile_tuning(profile: DeploymentProfile) -> ProfileTuning:
    """Resolve the ProfileTuning for a deployment profile.

    Args:
        profile: The deployment profile.

    Returns:
        Frozen ProfileTuning with all 5 domain slices.
    """
    mapping = _get_profile_tuning_map()
    tuning = mapping.get(profile)
    if tuning is None:
        raise ValueError(f"Unknown deployment profile: {profile!r}")
    return tuning
