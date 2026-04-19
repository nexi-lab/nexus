"""Per-profile performance tuning configuration (Issue #2071).

Migrates scattered hardcoded performance thresholds to deployment profiles.
Each DeploymentProfile maps to a ProfileTuning frozen dataclass composed
of 11 domain-specific tuning slices.

Pattern follows IOProfile (Issue #1413): frozen dataclasses, profile-selected,
wired via DI in factory.py.

Domain configs:
- ConcurrencyTuning: worker counts, thread pool sizes
- NetworkTuning: HTTP timeouts, webhook timeouts
- StorageTuning: write buffer, batch sizes, DB pool
- SearchTuning: grep workers, search concurrency
- CacheTuning: tiger cache workers, batch sizes
- BackgroundTaskTuning: cleanup intervals, heartbeat flush
- ResiliencyTuning: retry counts, circuit breaker thresholds
- ConnectorTuning: blob operation / upload timeouts, max workers
- PoolTuning: asyncpg, httpx, remote pool sizing
- EvictionTuning: agent eviction watermarks, batch sizes (Issue #2170)
- QoSTuning: agent QoS class configs for scheduling/eviction (Issue #2171)

Profile hierarchy matches DeploymentProfile:
    kernel ⊂ embedded ⊂ lite ⊂ full ⊆ cloud
    (bare)  (minimal)  (conservative)  (balanced)  (aggressive)
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.contracts.qos import QoSClassConfig, QoSTuning

if TYPE_CHECKING:
    from nexus.contracts.deployment_profile import DeploymentProfile

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

    Consumers: batch_executor, subscriptions/manager, mcp/mount.
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


@dataclass(frozen=True)
class BackgroundTaskTuning:
    """Intervals for periodic background tasks.

    Consumers: server/background_tasks, server/lifespan/services.
    """

    sandbox_cleanup_interval: int
    """Interval (seconds) between sandbox cleanup sweeps."""

    session_cleanup_interval: int
    """Interval (seconds) between inactive session cleanup sweeps."""

    daily_gc_interval: int
    """Interval (seconds) for daily garbage collection tasks."""

    heartbeat_flush_interval: int
    """Interval (seconds) between agent heartbeat flushes."""

    stale_agent_check_interval: int
    """Interval (seconds) between stale agent detection sweeps."""

    stale_agent_threshold: int
    """Seconds after which an agent is considered stale."""


@dataclass(frozen=True)
class ResiliencyTuning:
    """Default retry and circuit breaker thresholds.

    Consumers: core/resiliency, factory.py (ResiliencyConfig defaults).
    """

    default_max_retries: int
    """Default max retry attempts for transient failures."""

    retry_base_backoff_ms: int
    """Base backoff interval (milliseconds) for exponential retry."""

    circuit_breaker_failure_threshold: int
    """Number of consecutive failures before circuit opens."""

    circuit_breaker_timeout: float
    """Seconds the circuit stays open before half-open probe."""


@dataclass(frozen=True)
class ConnectorTuning:
    """Blob storage and connector timeout configuration.

    Consumers: backends/gcs, backends/gcs_connector, backends/s3_connector.
    """

    blob_operation_timeout: float
    """Timeout (seconds) for standard blob read/write/delete operations."""

    large_upload_timeout: float
    """Timeout (seconds) for large file uploads."""

    connector_max_workers: int
    """Max worker threads for connector parallel operations."""


@dataclass(frozen=True)
class EvictionTuning:
    """Agent eviction thresholds under resource pressure (Issue #2170).

    Consumers: services/agents/eviction_manager, services/agents/resource_monitor,
    server/background_tasks, server/lifespan/services.
    """

    memory_high_watermark_pct: int
    """Start evicting when memory usage exceeds this percentage."""

    memory_low_watermark_pct: int
    """Stop evicting when memory usage drops below this percentage."""

    max_active_agents: int
    """Hard cap on concurrently CONNECTED agents."""

    eviction_batch_size: int
    """Number of agents to evict per cycle."""

    checkpoint_timeout_seconds: float
    """Maximum time (seconds) allowed for checkpoint writes."""

    eviction_cooldown_seconds: int
    """Minimum seconds between eviction cycles."""

    eviction_poll_interval_seconds: int = 300
    """How often (seconds) the background task checks for eviction. Separate from
    cooldown — poll interval controls check frequency, cooldown prevents thrashing."""

    checkpoint_cleanup_interval_seconds: int = 3600
    """How often (seconds) to sweep stale checkpoint data from SUSPENDED agents."""

    checkpoint_max_age_seconds: int = 86400
    """Maximum age (seconds) for checkpoint data before cleanup removes it."""

    max_concurrent_transitions: int = 10
    """Semaphore limit for concurrent state transitions during eviction.
    Prevents connection pool exhaustion on large batch sizes."""


@dataclass(frozen=True)
class PoolTuning:
    """Connection pool sizing for asyncpg, httpx, and remote stores.

    Consumers: server/lifespan/services, server/fastapi_server,
    search/daemon, httpx client construction.
    """

    asyncpg_min_size: int
    """Minimum connections in asyncpg pool (scheduler, search)."""

    asyncpg_max_size: int
    """Maximum connections in asyncpg pool (scheduler, search)."""

    httpx_max_connections: int
    """Max connections for httpx async client pools."""

    remote_pool_maxsize: int
    """Max size for remote storage connection pools."""


# ---------------------------------------------------------------------------
# Composite tuning (all 10 domains)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileTuning:
    """Composite performance tuning for a deployment profile.

    Composed of 11 domain-specific slices.  Each brick receives only
    the slice it needs via constructor injection in factory.py.
    """

    concurrency: ConcurrencyTuning
    network: NetworkTuning
    storage: StorageTuning
    search: SearchTuning
    cache: CacheTuning
    background_task: BackgroundTaskTuning
    resiliency: ResiliencyTuning
    connector: ConnectorTuning
    pool: PoolTuning
    eviction: EvictionTuning
    qos: QoSTuning


# ---------------------------------------------------------------------------
# Profile-to-tuning mappings (frozen — immutable at runtime)
# ---------------------------------------------------------------------------

_SLIM_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=1,
        thread_pool_size=4,
        max_async_concurrency=1,
        task_runner_workers=1,
    ),
    network=NetworkTuning(
        default_http_timeout=15.0,
        webhook_timeout=5.0,
        long_operation_timeout=30.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=500,
        write_buffer_max_size=5,
        changelog_chunk_size=25,
        db_pool_size=1,
        db_max_overflow=2,
    ),
    search=SearchTuning(
        grep_parallel_workers=1,
        list_parallel_workers=1,
        search_max_concurrency=1,
        vector_pool_workers=1,
    ),
    cache=CacheTuning(
        tiger_max_workers=1,
        tiger_batch_size=25,
    ),
    background_task=BackgroundTaskTuning(
        sandbox_cleanup_interval=900,
        session_cleanup_interval=14400,
        daily_gc_interval=86400,
        heartbeat_flush_interval=300,
        stale_agent_check_interval=900,
        stale_agent_threshold=900,
    ),
    resiliency=ResiliencyTuning(
        default_max_retries=1,
        retry_base_backoff_ms=50,
        circuit_breaker_failure_threshold=3,
        circuit_breaker_timeout=15.0,
    ),
    connector=ConnectorTuning(
        blob_operation_timeout=30.0,
        large_upload_timeout=120.0,
        connector_max_workers=1,
    ),
    pool=PoolTuning(
        asyncpg_min_size=1,
        asyncpg_max_size=1,
        httpx_max_connections=5,
        remote_pool_maxsize=2,
    ),
    eviction=EvictionTuning(
        memory_high_watermark_pct=95,
        memory_low_watermark_pct=90,
        max_active_agents=10,
        eviction_batch_size=2,
        checkpoint_timeout_seconds=3.0,
        eviction_cooldown_seconds=300,
        eviction_poll_interval_seconds=900,
        checkpoint_cleanup_interval_seconds=14400,
        checkpoint_max_age_seconds=86400,
        max_concurrent_transitions=2,
    ),
    qos=QoSTuning(
        premium=QoSClassConfig(
            max_concurrent_tasks=3, scheduling_weight=3, eviction_priority=2, preemptible=False
        ),
        standard=QoSClassConfig(
            max_concurrent_tasks=1, scheduling_weight=1, eviction_priority=1, preemptible=False
        ),
        spot=QoSClassConfig(
            max_concurrent_tasks=1, scheduling_weight=1, eviction_priority=0, preemptible=True
        ),
    ),
)

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
        db_pool_size=3,
        db_max_overflow=5,
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
    background_task=BackgroundTaskTuning(
        sandbox_cleanup_interval=600,
        session_cleanup_interval=7200,
        daily_gc_interval=86400,
        heartbeat_flush_interval=120,
        stale_agent_check_interval=600,
        stale_agent_threshold=600,
    ),
    resiliency=ResiliencyTuning(
        default_max_retries=2,
        retry_base_backoff_ms=50,
        circuit_breaker_failure_threshold=3,
        circuit_breaker_timeout=15.0,
    ),
    connector=ConnectorTuning(
        blob_operation_timeout=30.0,
        large_upload_timeout=120.0,
        connector_max_workers=2,
    ),
    pool=PoolTuning(
        asyncpg_min_size=1,
        asyncpg_max_size=2,
        httpx_max_connections=10,
        remote_pool_maxsize=5,
    ),
    eviction=EvictionTuning(
        memory_high_watermark_pct=90,
        memory_low_watermark_pct=85,
        max_active_agents=50,
        eviction_batch_size=5,
        checkpoint_timeout_seconds=5.0,
        eviction_cooldown_seconds=120,
        eviction_poll_interval_seconds=600,
        checkpoint_cleanup_interval_seconds=7200,
        checkpoint_max_age_seconds=86400,
        max_concurrent_transitions=5,
    ),
    qos=QoSTuning(
        premium=QoSClassConfig(
            max_concurrent_tasks=5, scheduling_weight=3, eviction_priority=2, preemptible=False
        ),
        standard=QoSClassConfig(
            max_concurrent_tasks=3, scheduling_weight=1, eviction_priority=1, preemptible=False
        ),
        spot=QoSClassConfig(
            max_concurrent_tasks=1, scheduling_weight=1, eviction_priority=0, preemptible=True
        ),
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
        db_pool_size=8,
        db_max_overflow=15,
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
    background_task=BackgroundTaskTuning(
        sandbox_cleanup_interval=600,
        session_cleanup_interval=7200,
        daily_gc_interval=86400,
        heartbeat_flush_interval=120,
        stale_agent_check_interval=600,
        stale_agent_threshold=600,
    ),
    resiliency=ResiliencyTuning(
        default_max_retries=3,
        retry_base_backoff_ms=50,
        circuit_breaker_failure_threshold=5,
        circuit_breaker_timeout=30.0,
    ),
    connector=ConnectorTuning(
        blob_operation_timeout=60.0,
        large_upload_timeout=300.0,
        connector_max_workers=5,
    ),
    pool=PoolTuning(
        asyncpg_min_size=2,
        asyncpg_max_size=5,
        httpx_max_connections=50,
        remote_pool_maxsize=10,
    ),
    eviction=EvictionTuning(
        memory_high_watermark_pct=85,
        memory_low_watermark_pct=80,
        max_active_agents=200,
        eviction_batch_size=10,
        checkpoint_timeout_seconds=5.0,
        eviction_cooldown_seconds=90,
        eviction_poll_interval_seconds=300,
        checkpoint_cleanup_interval_seconds=3600,
        checkpoint_max_age_seconds=86400,
        max_concurrent_transitions=10,
    ),
    qos=QoSTuning(
        premium=QoSClassConfig(
            max_concurrent_tasks=10, scheduling_weight=3, eviction_priority=2, preemptible=False
        ),
        standard=QoSClassConfig(
            max_concurrent_tasks=5, scheduling_weight=1, eviction_priority=1, preemptible=False
        ),
        spot=QoSClassConfig(
            max_concurrent_tasks=2, scheduling_weight=1, eviction_priority=0, preemptible=True
        ),
    ),
)

_SANDBOX_TUNING = ProfileTuning(
    concurrency=ConcurrencyTuning(
        default_workers=2,
        thread_pool_size=8,
        max_async_concurrency=4,
        task_runner_workers=2,
    ),
    network=NetworkTuning(
        default_http_timeout=10.0,
        webhook_timeout=5.0,
        long_operation_timeout=30.0,
    ),
    storage=StorageTuning(
        write_buffer_flush_ms=100,
        write_buffer_max_size=50,
        changelog_chunk_size=100,
        db_pool_size=2,
        db_max_overflow=2,
    ),
    search=SearchTuning(
        grep_parallel_workers=2,
        list_parallel_workers=2,
        search_max_concurrency=2,
        vector_pool_workers=0,  # no local vector backend
    ),
    cache=CacheTuning(
        tiger_max_workers=1,
        tiger_batch_size=20,
    ),
    # Reuse LITE values for remaining slices
    background_task=_LITE_TUNING.background_task,
    resiliency=_LITE_TUNING.resiliency,
    connector=_LITE_TUNING.connector,
    pool=PoolTuning(
        # SANDBOX is SQLite-only; asyncpg is never created (scheduler skips when
        # database_url is unset). Values kept at 1/1 to satisfy the universal
        # "tuning values must be positive" invariant in tests/unit/core/
        # test_performance_tuning.py — zero would be accurate but breaks the shared
        # validator and these values are never read at runtime on SANDBOX.
        asyncpg_min_size=1,
        asyncpg_max_size=1,
        httpx_max_connections=10,
        remote_pool_maxsize=10,
    ),
    eviction=_LITE_TUNING.eviction,
    qos=_LITE_TUNING.qos,
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
        db_pool_size=20,
        db_max_overflow=30,
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
    background_task=BackgroundTaskTuning(
        sandbox_cleanup_interval=300,
        session_cleanup_interval=3600,
        daily_gc_interval=86400,
        heartbeat_flush_interval=60,
        stale_agent_check_interval=300,
        stale_agent_threshold=300,
    ),
    resiliency=ResiliencyTuning(
        default_max_retries=3,
        retry_base_backoff_ms=50,
        circuit_breaker_failure_threshold=5,
        circuit_breaker_timeout=30.0,
    ),
    connector=ConnectorTuning(
        blob_operation_timeout=60.0,
        large_upload_timeout=300.0,
        connector_max_workers=20,
    ),
    pool=PoolTuning(
        asyncpg_min_size=2,
        asyncpg_max_size=5,
        httpx_max_connections=100,
        remote_pool_maxsize=20,
    ),
    eviction=EvictionTuning(
        memory_high_watermark_pct=85,
        memory_low_watermark_pct=75,
        max_active_agents=1000,
        eviction_batch_size=20,
        checkpoint_timeout_seconds=10.0,
        eviction_cooldown_seconds=60,
        eviction_poll_interval_seconds=120,
        checkpoint_cleanup_interval_seconds=3600,
        checkpoint_max_age_seconds=86400,
        max_concurrent_transitions=10,
    ),
    qos=QoSTuning(
        premium=QoSClassConfig(
            max_concurrent_tasks=20, scheduling_weight=3, eviction_priority=2, preemptible=False
        ),
        standard=QoSClassConfig(
            max_concurrent_tasks=10, scheduling_weight=1, eviction_priority=1, preemptible=False
        ),
        spot=QoSClassConfig(
            max_concurrent_tasks=5, scheduling_weight=1, eviction_priority=0, preemptible=True
        ),
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
        db_pool_size=30,
        db_max_overflow=50,
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
    background_task=BackgroundTaskTuning(
        sandbox_cleanup_interval=300,
        session_cleanup_interval=1800,
        daily_gc_interval=86400,
        heartbeat_flush_interval=30,
        stale_agent_check_interval=120,
        stale_agent_threshold=120,
    ),
    resiliency=ResiliencyTuning(
        default_max_retries=5,
        retry_base_backoff_ms=100,
        circuit_breaker_failure_threshold=8,
        circuit_breaker_timeout=60.0,
    ),
    connector=ConnectorTuning(
        blob_operation_timeout=120.0,
        large_upload_timeout=600.0,
        connector_max_workers=40,
    ),
    pool=PoolTuning(
        asyncpg_min_size=5,
        asyncpg_max_size=15,
        httpx_max_connections=200,
        remote_pool_maxsize=50,
    ),
    eviction=EvictionTuning(
        memory_high_watermark_pct=80,
        memory_low_watermark_pct=70,
        max_active_agents=10000,
        eviction_batch_size=50,
        checkpoint_timeout_seconds=10.0,
        eviction_cooldown_seconds=30,
        eviction_poll_interval_seconds=60,
        checkpoint_cleanup_interval_seconds=1800,
        checkpoint_max_age_seconds=43200,
        max_concurrent_transitions=20,
    ),
    qos=QoSTuning(
        premium=QoSClassConfig(
            max_concurrent_tasks=50, scheduling_weight=3, eviction_priority=2, preemptible=False
        ),
        standard=QoSClassConfig(
            max_concurrent_tasks=20, scheduling_weight=1, eviction_priority=1, preemptible=False
        ),
        spot=QoSClassConfig(
            max_concurrent_tasks=10, scheduling_weight=1, eviction_priority=0, preemptible=True
        ),
    ),
)


def _get_profile_tuning_map() -> dict[str, ProfileTuning]:
    """Build profile-to-tuning mapping (lazy import to avoid circular)."""
    from nexus.contracts.deployment_profile import DeploymentProfile

    return {
        DeploymentProfile.SLIM: _SLIM_TUNING,
        DeploymentProfile.CLUSTER: _SLIM_TUNING,  # CLUSTER reuses SLIM tuning
        DeploymentProfile.EMBEDDED: _EMBEDDED_TUNING,
        DeploymentProfile.LITE: _LITE_TUNING,
        DeploymentProfile.SANDBOX: _SANDBOX_TUNING,
        DeploymentProfile.FULL: _FULL_TUNING,
        DeploymentProfile.CLOUD: _CLOUD_TUNING,
        DeploymentProfile.REMOTE: _SLIM_TUNING,  # REMOTE reuses SLIM tuning
    }


def resolve_profile_tuning(profile: "DeploymentProfile") -> ProfileTuning:
    """Resolve the ProfileTuning for a deployment profile.

    Args:
        profile: The deployment profile.

    Returns:
        Frozen ProfileTuning with all 11 domain slices.
    """
    mapping = _get_profile_tuning_map()
    tuning = mapping.get(profile)
    if tuning is None:
        raise ValueError(f"Unknown deployment profile: {profile!r}")
    return tuning
