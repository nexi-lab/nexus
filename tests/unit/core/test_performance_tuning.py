"""Tests for per-profile performance tuning (Issue #2071).

Tests cover:
- All 4 profiles produce correct frozen ProfileTuning
- Hierarchy monotonicity (embedded ≤ lite ≤ full ≤ cloud for resource fields)
- Frozen immutability (cannot mutate at runtime)
- DeploymentProfile.tuning() integration
- resolve_profile_tuning() with invalid input
- Individual domain dataclass field validation
"""

from __future__ import annotations

import pytest

from nexus.core.deployment_profile import DeploymentProfile
from nexus.core.performance_tuning import (
    CacheTuning,
    ConcurrencyTuning,
    NetworkTuning,
    ProfileTuning,
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
        assert s.db_pool_size == 10
        assert s.db_max_overflow == 20

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

    def test_embedded_minimal(self) -> None:
        """Embedded should have the smallest resource allocation."""
        t = DeploymentProfile.EMBEDDED.tuning()
        assert t.concurrency.default_workers == 1
        assert t.concurrency.thread_pool_size == 10
        assert t.storage.db_pool_size == 2

    def test_cloud_aggressive(self) -> None:
        """Cloud should have the largest resource allocation."""
        t = DeploymentProfile.CLOUD.tuning()
        assert t.concurrency.default_workers == 8
        assert t.concurrency.thread_pool_size == 400
        assert t.storage.db_pool_size == 20
        assert t.search.search_max_concurrency == 20


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


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Invalid inputs must fail fast with clear errors."""

    def test_invalid_profile_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown deployment profile"):
            resolve_profile_tuning("nonexistent")  # type: ignore[arg-type]
