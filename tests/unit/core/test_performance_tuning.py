"""Tests for per-profile performance tuning (Issue #2071).

Tests cover:
- All 4 profiles produce correct frozen ProfileTuning with 10 slices
- Hierarchy monotonicity (embedded ≤ lite ≤ full ≤ cloud for resource fields)
- Frozen immutability (cannot mutate at runtime)
- DeploymentProfile.tuning() integration
- resolve_profile_tuning() with invalid input
- Individual domain dataclass field validation
- Snapshot tests: FULL profile matches previous hardcoded defaults
"""

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.lib.performance_tuning import (
    BackgroundTaskTuning,
    CacheTuning,
    ConcurrencyTuning,
    ConnectorTuning,
    EvictionTuning,
    NetworkTuning,
    PoolTuning,
    ProfileTuning,
    ResiliencyTuning,
    SearchTuning,
    StorageTuning,
    resolve_profile_tuning,
)

# ---------------------------------------------------------------------------
# All profiles produce valid tuning
# ---------------------------------------------------------------------------


class TestAllProfilesHaveTuning:
    """Every DeploymentProfile must produce a valid ProfileTuning."""

    @pytest.mark.parametrize("profile", list(DeploymentProfile))
    def test_profile_has_tuning(self, profile: DeploymentProfile) -> None:
        tuning = resolve_profile_tuning(profile)
        assert isinstance(tuning, ProfileTuning)
        assert isinstance(tuning.concurrency, ConcurrencyTuning)
        assert isinstance(tuning.network, NetworkTuning)
        assert isinstance(tuning.storage, StorageTuning)
        assert isinstance(tuning.search, SearchTuning)
        assert isinstance(tuning.cache, CacheTuning)
        assert isinstance(tuning.background_task, BackgroundTaskTuning)
        assert isinstance(tuning.resiliency, ResiliencyTuning)
        assert isinstance(tuning.connector, ConnectorTuning)
        assert isinstance(tuning.pool, PoolTuning)
        assert isinstance(tuning.eviction, EvictionTuning)

    @pytest.mark.parametrize("profile", list(DeploymentProfile))
    def test_tuning_via_enum_method(self, profile: DeploymentProfile) -> None:
        """DeploymentProfile.tuning() delegates correctly."""
        tuning = profile.tuning()
        assert isinstance(tuning, ProfileTuning)
        # Should return same object each call (module-level singletons)
        assert tuning is profile.tuning()

    @pytest.mark.parametrize("profile", list(DeploymentProfile))
    def test_all_values_positive(self, profile: DeploymentProfile) -> None:
        """All numeric tuning values must be positive."""
        tuning = profile.tuning()
        for field_name in (
            "default_workers",
            "thread_pool_size",
            "max_async_concurrency",
            "task_runner_workers",
        ):
            assert getattr(tuning.concurrency, field_name) > 0, (
                f"{profile}.concurrency.{field_name} must be positive"
            )
        for field_name in ("default_http_timeout", "webhook_timeout", "long_operation_timeout"):
            assert getattr(tuning.network, field_name) > 0, (
                f"{profile}.network.{field_name} must be positive"
            )
        for field_name in (
            "write_buffer_flush_ms",
            "write_buffer_max_size",
            "changelog_chunk_size",
            "db_pool_size",
            "db_max_overflow",
        ):
            assert getattr(tuning.storage, field_name) > 0, (
                f"{profile}.storage.{field_name} must be positive"
            )
        for field_name in (
            "sandbox_cleanup_interval",
            "session_cleanup_interval",
            "daily_gc_interval",
            "heartbeat_flush_interval",
            "stale_agent_check_interval",
            "stale_agent_threshold",
        ):
            assert getattr(tuning.background_task, field_name) > 0, (
                f"{profile}.background_task.{field_name} must be positive"
            )
        for field_name in (
            "default_max_retries",
            "retry_base_backoff_ms",
            "circuit_breaker_failure_threshold",
            "circuit_breaker_timeout",
        ):
            assert getattr(tuning.resiliency, field_name) > 0, (
                f"{profile}.resiliency.{field_name} must be positive"
            )
        for field_name in (
            "blob_operation_timeout",
            "large_upload_timeout",
            "connector_max_workers",
        ):
            assert getattr(tuning.connector, field_name) > 0, (
                f"{profile}.connector.{field_name} must be positive"
            )
        # Issue #3778: SANDBOX is SQLite-only; asyncpg is never created
        # (scheduler skips when database_url is unset). Allow zero asyncpg
        # values on that profile — covered by the dedicated assertion in
        # tests/unit/core/test_sandbox_profile.py::test_tuning_disables_asyncpg_pool.
        _pool_fields = ("httpx_max_connections", "remote_pool_maxsize")
        if profile != DeploymentProfile.SANDBOX:
            _pool_fields = ("asyncpg_min_size", "asyncpg_max_size", *_pool_fields)
        for field_name in _pool_fields:
            assert getattr(tuning.pool, field_name) > 0, (
                f"{profile}.pool.{field_name} must be positive"
            )


# ---------------------------------------------------------------------------
# Hierarchy monotonicity: embedded ≤ lite ≤ full ≤ cloud
# ---------------------------------------------------------------------------


class TestHierarchyMonotonicity:
    """Resource-scaling fields must not decrease as profiles grow."""

    PROFILES_ORDERED = [
        DeploymentProfile.EMBEDDED,
        DeploymentProfile.LITE,
        DeploymentProfile.FULL,
        DeploymentProfile.CLOUD,
    ]

    @pytest.mark.parametrize(
        "field",
        [
            "default_workers",
            "thread_pool_size",
            "max_async_concurrency",
            "task_runner_workers",
        ],
    )
    def test_concurrency_monotonic(self, field: str) -> None:
        values = [getattr(p.tuning().concurrency, field) for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"concurrency.{field}: {self.PROFILES_ORDERED[i]}={values[i]} > "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    @pytest.mark.parametrize(
        "field",
        [
            "grep_parallel_workers",
            "list_parallel_workers",
            "search_max_concurrency",
            "vector_pool_workers",
        ],
    )
    def test_search_monotonic(self, field: str) -> None:
        values = [getattr(p.tuning().search, field) for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"search.{field}: {self.PROFILES_ORDERED[i]}={values[i]} > "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    @pytest.mark.parametrize(
        "field",
        [
            "db_pool_size",
            "db_max_overflow",
            "write_buffer_max_size",
            "changelog_chunk_size",
        ],
    )
    def test_storage_monotonic(self, field: str) -> None:
        values = [getattr(p.tuning().storage, field) for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"storage.{field}: {self.PROFILES_ORDERED[i]}={values[i]} > "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    @pytest.mark.parametrize("field", ["tiger_max_workers", "tiger_batch_size"])
    def test_cache_monotonic(self, field: str) -> None:
        values = [getattr(p.tuning().cache, field) for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"cache.{field}: {self.PROFILES_ORDERED[i]}={values[i]} > "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    def test_write_buffer_flush_ms_decreases(self) -> None:
        """Flush interval should DECREASE as profile grows (faster flushing)."""
        values = [p.tuning().storage.write_buffer_flush_ms for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] >= values[i + 1], (
                f"storage.write_buffer_flush_ms should decrease: "
                f"{self.PROFILES_ORDERED[i]}={values[i]} < "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    # --- New slice monotonicity ---

    def test_heartbeat_flush_interval_decreases(self) -> None:
        """Heartbeat interval should DECREASE (more frequent in bigger profiles)."""
        values = [
            p.tuning().background_task.heartbeat_flush_interval for p in self.PROFILES_ORDERED
        ]
        for i in range(len(values) - 1):
            assert values[i] >= values[i + 1], (
                f"background_task.heartbeat_flush_interval should decrease: "
                f"{self.PROFILES_ORDERED[i]}={values[i]} < "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    @pytest.mark.parametrize(
        "field",
        [
            "default_max_retries",
            "circuit_breaker_failure_threshold",
            "circuit_breaker_timeout",
        ],
    )
    def test_resiliency_monotonic(self, field: str) -> None:
        values = [getattr(p.tuning().resiliency, field) for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"resiliency.{field}: {self.PROFILES_ORDERED[i]}={values[i]} > "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    @pytest.mark.parametrize(
        "field",
        [
            "blob_operation_timeout",
            "large_upload_timeout",
            "connector_max_workers",
        ],
    )
    def test_connector_monotonic(self, field: str) -> None:
        values = [getattr(p.tuning().connector, field) for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"connector.{field}: {self.PROFILES_ORDERED[i]}={values[i]} > "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )

    @pytest.mark.parametrize(
        "field",
        [
            "asyncpg_min_size",
            "asyncpg_max_size",
            "httpx_max_connections",
            "remote_pool_maxsize",
        ],
    )
    def test_pool_monotonic(self, field: str) -> None:
        values = [getattr(p.tuning().pool, field) for p in self.PROFILES_ORDERED]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"pool.{field}: {self.PROFILES_ORDERED[i]}={values[i]} > "
                f"{self.PROFILES_ORDERED[i + 1]}={values[i + 1]}"
            )


# ---------------------------------------------------------------------------
# Concrete value assertions (catches accidental changes)
# ---------------------------------------------------------------------------


class TestConcreteValues:
    """Assert specific values for FULL profile (the most common default)."""

    def test_full_concurrency(self) -> None:
        c = DeploymentProfile.FULL.tuning().concurrency
        assert c.default_workers == 4
        assert c.thread_pool_size == 200
        assert c.max_async_concurrency == 10
        assert c.task_runner_workers == 4

    def test_full_network(self) -> None:
        n = DeploymentProfile.FULL.tuning().network
        assert n.default_http_timeout == 30.0
        assert n.webhook_timeout == 10.0
        assert n.long_operation_timeout == 120.0

    def test_full_storage(self) -> None:
        s = DeploymentProfile.FULL.tuning().storage
        assert s.write_buffer_flush_ms == 100
        assert s.write_buffer_max_size == 100
        assert s.changelog_chunk_size == 500
        assert s.db_pool_size == 20
        assert s.db_max_overflow == 30

    def test_full_search(self) -> None:
        s = DeploymentProfile.FULL.tuning().search
        assert s.grep_parallel_workers == 4
        assert s.list_parallel_workers == 10
        assert s.search_max_concurrency == 10
        assert s.vector_pool_workers == 2

    def test_full_cache(self) -> None:
        c = DeploymentProfile.FULL.tuning().cache
        assert c.tiger_max_workers == 4
        assert c.tiger_batch_size == 100

    def test_full_background_task(self) -> None:
        bt = DeploymentProfile.FULL.tuning().background_task
        assert bt.sandbox_cleanup_interval == 300
        assert bt.session_cleanup_interval == 3600
        assert bt.daily_gc_interval == 86400
        assert bt.heartbeat_flush_interval == 60
        assert bt.stale_agent_check_interval == 300
        assert bt.stale_agent_threshold == 300

    def test_full_resiliency(self) -> None:
        r = DeploymentProfile.FULL.tuning().resiliency
        assert r.default_max_retries == 3
        assert r.retry_base_backoff_ms == 50
        assert r.circuit_breaker_failure_threshold == 5
        assert r.circuit_breaker_timeout == 30.0

    def test_full_connector(self) -> None:
        cn = DeploymentProfile.FULL.tuning().connector
        assert cn.blob_operation_timeout == 60.0
        assert cn.large_upload_timeout == 300.0
        assert cn.connector_max_workers == 20

    def test_full_pool(self) -> None:
        p = DeploymentProfile.FULL.tuning().pool
        assert p.asyncpg_min_size == 2
        assert p.asyncpg_max_size == 5
        assert p.httpx_max_connections == 100
        assert p.remote_pool_maxsize == 20

    def test_embedded_minimal(self) -> None:
        """Embedded should have the smallest resource allocation."""
        t = DeploymentProfile.EMBEDDED.tuning()
        assert t.concurrency.default_workers == 1
        assert t.concurrency.thread_pool_size == 10
        assert t.storage.db_pool_size == 3

    def test_cloud_aggressive(self) -> None:
        """Cloud should have the largest resource allocation."""
        t = DeploymentProfile.CLOUD.tuning()
        assert t.concurrency.default_workers == 8
        assert t.concurrency.thread_pool_size == 400
        assert t.storage.db_pool_size == 30
        assert t.search.search_max_concurrency == 20


# ---------------------------------------------------------------------------
# Snapshot tests: FULL profile matches previous hardcoded defaults
# ---------------------------------------------------------------------------


class TestFullProfileMatchesPreviousDefaults:
    """FULL profile values must match the previous hardcoded defaults.

    These snapshot assertions ensure that migrating hardcoded values to
    ProfileTuning doesn't accidentally change runtime behavior.
    """

    def test_db_pool_size_matches_record_store_default(self) -> None:
        """record_store.py _build_pool_kwargs default_pool_size=20."""
        assert DeploymentProfile.FULL.tuning().storage.db_pool_size == 20

    def test_db_max_overflow_matches_record_store_default(self) -> None:
        """record_store.py _build_pool_kwargs default_max_overflow=30."""
        assert DeploymentProfile.FULL.tuning().storage.db_max_overflow == 30

    def test_task_runner_workers_matches_lifespan_default(self) -> None:
        """services.py AsyncTaskRunner max_workers=4."""
        assert DeploymentProfile.FULL.tuning().concurrency.task_runner_workers == 4

    def test_asyncpg_pool_matches_lifespan_default(self) -> None:
        """services.py asyncpg.create_pool min_size=2, max_size=5."""
        p = DeploymentProfile.FULL.tuning().pool
        assert p.asyncpg_min_size == 2
        assert p.asyncpg_max_size == 5

    def test_blob_timeout_matches_gcs_default(self) -> None:
        """gcs.py blob.upload/download timeout=60."""
        assert DeploymentProfile.FULL.tuning().connector.blob_operation_timeout == 60.0

    def test_large_upload_timeout_matches_gcs_default(self) -> None:
        """gcs.py blob.upload_from_file timeout=300."""
        assert DeploymentProfile.FULL.tuning().connector.large_upload_timeout == 300.0

    def test_webhook_timeout_matches_manager_default(self) -> None:
        """subscriptions/manager.py WEBHOOK_TIMEOUT=10.0."""
        assert DeploymentProfile.FULL.tuning().network.webhook_timeout == 10.0

    def test_batch_executor_timeout_matches_default(self) -> None:
        """batch_executor.py DEFAULT_OPERATION_TIMEOUT=30.0."""
        assert DeploymentProfile.FULL.tuning().network.default_http_timeout == 30.0

    def test_grep_workers_matches_strategies_default(self) -> None:
        """strategies.py GREP_PARALLEL_WORKERS=4."""
        assert DeploymentProfile.FULL.tuning().search.grep_parallel_workers == 4

    def test_list_workers_matches_search_service_default(self) -> None:
        """search_service.py LIST_PARALLEL_WORKERS=10."""
        assert DeploymentProfile.FULL.tuning().search.list_parallel_workers == 10

    def test_sandbox_cleanup_matches_background_default(self) -> None:
        """background_tasks.py sandbox_cleanup interval_seconds=300."""
        assert DeploymentProfile.FULL.tuning().background_task.sandbox_cleanup_interval == 300

    def test_session_cleanup_matches_background_default(self) -> None:
        """background_tasks.py session_cleanup interval_seconds=3600."""
        assert DeploymentProfile.FULL.tuning().background_task.session_cleanup_interval == 3600

    def test_heartbeat_flush_matches_lifespan_default(self) -> None:
        """services.py heartbeat_flush_task interval_seconds=60."""
        assert DeploymentProfile.FULL.tuning().background_task.heartbeat_flush_interval == 60

    def test_stale_agent_check_matches_lifespan_default(self) -> None:
        """services.py stale_agent_detection_task interval_seconds=300."""
        assert DeploymentProfile.FULL.tuning().background_task.stale_agent_check_interval == 300

    def test_tiger_max_workers_matches_bitmap_cache_default(self) -> None:
        """bitmap_cache.py ThreadPoolExecutor max_workers=4."""
        assert DeploymentProfile.FULL.tuning().cache.tiger_max_workers == 4


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestFrozenImmutability:
    """Tuning dataclasses must be frozen (immutable)."""

    def test_profile_tuning_frozen(self) -> None:
        tuning = DeploymentProfile.FULL.tuning()
        with pytest.raises(AttributeError):
            tuning.concurrency = ConcurrencyTuning(1, 1, 1, 1)  # type: ignore[misc]

    def test_concurrency_tuning_frozen(self) -> None:
        c = DeploymentProfile.FULL.tuning().concurrency
        with pytest.raises(AttributeError):
            c.default_workers = 999  # type: ignore[misc]

    def test_network_tuning_frozen(self) -> None:
        n = DeploymentProfile.FULL.tuning().network
        with pytest.raises(AttributeError):
            n.default_http_timeout = 999  # type: ignore[misc]

    def test_storage_tuning_frozen(self) -> None:
        s = DeploymentProfile.FULL.tuning().storage
        with pytest.raises(AttributeError):
            s.db_pool_size = 999  # type: ignore[misc]

    def test_search_tuning_frozen(self) -> None:
        s = DeploymentProfile.FULL.tuning().search
        with pytest.raises(AttributeError):
            s.grep_parallel_workers = 999  # type: ignore[misc]

    def test_cache_tuning_frozen(self) -> None:
        c = DeploymentProfile.FULL.tuning().cache
        with pytest.raises(AttributeError):
            c.tiger_max_workers = 999  # type: ignore[misc]

    def test_background_task_tuning_frozen(self) -> None:
        bt = DeploymentProfile.FULL.tuning().background_task
        with pytest.raises(AttributeError):
            bt.sandbox_cleanup_interval = 999  # type: ignore[misc]

    def test_resiliency_tuning_frozen(self) -> None:
        r = DeploymentProfile.FULL.tuning().resiliency
        with pytest.raises(AttributeError):
            r.default_max_retries = 999  # type: ignore[misc]

    def test_connector_tuning_frozen(self) -> None:
        cn = DeploymentProfile.FULL.tuning().connector
        with pytest.raises(AttributeError):
            cn.blob_operation_timeout = 999  # type: ignore[misc]

    def test_pool_tuning_frozen(self) -> None:
        p = DeploymentProfile.FULL.tuning().pool
        with pytest.raises(AttributeError):
            p.asyncpg_min_size = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Invalid inputs must fail fast with clear errors."""

    def test_invalid_profile_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown deployment profile"):
            resolve_profile_tuning("nonexistent")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Eviction tuning tests (Issue #2170)
# ---------------------------------------------------------------------------


class TestEvictionTuning:
    """Tests for EvictionTuning across profiles."""

    @pytest.mark.parametrize("profile", list(DeploymentProfile))
    def test_eviction_tuning_all_profiles_valid(self, profile: DeploymentProfile) -> None:
        """All profiles must have valid eviction tuning with positive values."""
        e = profile.tuning().eviction
        assert isinstance(e, EvictionTuning)
        assert 0 < e.memory_low_watermark_pct < e.memory_high_watermark_pct <= 100
        assert e.max_active_agents > 0
        assert e.eviction_batch_size > 0
        assert e.checkpoint_timeout_seconds > 0
        assert e.eviction_cooldown_seconds > 0

    def test_eviction_tuning_embedded_conservative(self) -> None:
        """Embedded profile should have conservative eviction settings."""
        e = DeploymentProfile.EMBEDDED.tuning().eviction
        assert e.memory_high_watermark_pct == 90
        assert e.max_active_agents == 50
        assert e.eviction_batch_size == 5
        assert e.eviction_cooldown_seconds == 120

    def test_eviction_tuning_cloud_aggressive(self) -> None:
        """Cloud profile should have aggressive eviction settings."""
        e = DeploymentProfile.CLOUD.tuning().eviction
        assert e.memory_high_watermark_pct == 80
        assert e.max_active_agents == 10000
        assert e.eviction_batch_size == 50
        assert e.eviction_cooldown_seconds == 30

    def test_eviction_tuning_frozen(self) -> None:
        """EvictionTuning should be frozen (immutable)."""
        e = DeploymentProfile.FULL.tuning().eviction
        with pytest.raises(AttributeError):
            e.memory_high_watermark_pct = 999  # type: ignore[misc]
