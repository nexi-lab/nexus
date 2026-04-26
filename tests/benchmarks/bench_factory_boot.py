"""Benchmark: factory boot time for create_nexus_services().

Measures the wall-clock construction cost of the 3-tier boot sequence
(kernel, system, brick) with all heavy I/O mocked out. This ensures
that service wiring stays fast as more bricks are added.

Run with pytest-benchmark:
    PYTHONPATH=src python -m pytest tests/benchmarks/bench_factory_boot.py -v -s -o "addopts="

Works without pytest-benchmark (falls back to time.perf_counter loops).

Issue #2193: Kernel tier is now validation-only (empty dict). Former-kernel
services moved to system tier.
"""

import time
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Mock factory — builds the minimal mock graph that satisfies the factory
# ---------------------------------------------------------------------------

_BOOT_TIME_BUDGET_MS = 500
"""Maximum acceptable boot time in milliseconds."""

_WARMUP_ROUNDS = 2
"""Number of warmup iterations before measurement (JIT, import caching)."""

_MEASURE_ROUNDS = 5
"""Number of measured iterations for fallback (non-benchmark) mode."""


def _make_mock_record_store() -> MagicMock:
    """Build a RecordStoreABC mock with the attributes the factory reads."""
    store = MagicMock(name="record_store")
    store.engine = MagicMock(name="engine")
    store.read_engine = MagicMock(name="read_engine")
    store.session_factory = MagicMock(name="session_factory")
    store.async_session_factory = MagicMock(name="async_session_factory")
    store.has_read_replica = False
    store.database_url = "sqlite://"
    return store


def _make_mock_metadata_store() -> MagicMock:
    """Build a MetastoreABC mock."""
    return MagicMock(name="metadata_store")


def _make_mock_backend() -> MagicMock:
    """Build a Backend mock with the attributes the factory reads."""
    backend = MagicMock(name="backend")
    backend.root_path = "/tmp/bench_factory"
    type(backend).has_root_path = PropertyMock(return_value=True)
    backend.on_write_callback = None
    backend.on_sync_callback = None
    return backend


def _make_mock_dlc() -> MagicMock:
    """Build a DriverLifecycleCoordinator mock."""
    return MagicMock(name="dlc")


def _make_boot_context() -> Any:
    """Build a _BootContext with fully mocked deps.

    Imports are deferred so that a missing factory module causes a clear
    ImportError at call time, not at module-level collection.
    """
    from nexus.contracts.deployment_profile import DeploymentProfile
    from nexus.contracts.types import AuditConfig
    from nexus.core.config import (
        CacheConfig,
        DistributedConfig,
        PermissionConfig,
    )
    from nexus.factory import _BootContext
    from nexus.lib.performance_tuning import resolve_profile_tuning

    perm = PermissionConfig()
    audit = AuditConfig()
    cache_cfg = CacheConfig()
    dist = DistributedConfig()
    profile_tuning = resolve_profile_tuning(DeploymentProfile.FULL)

    record_store = _make_mock_record_store()

    return _BootContext(
        record_store=record_store,
        metadata_store=_make_mock_metadata_store(),
        backend=_make_mock_backend(),
        dlc=_make_mock_dlc(),
        engine=record_store.engine,
        read_engine=record_store.read_engine,
        perm=perm,
        audit=audit,
        cache_ttl_seconds=cache_cfg.ttl_seconds,
        dist=dist,
        zone_id="bench_zone",
        agent_id="bench_agent",
        enable_write_buffer=False,
        resiliency_raw=None,
        db_url="sqlite://",
        profile_tuning=profile_tuning,
    )


# ---------------------------------------------------------------------------
# Fallback timer — used when pytest-benchmark is not installed
# ---------------------------------------------------------------------------


class _FallbackBenchmark:
    """Minimal benchmark runner using time.perf_counter.

    Provides the same ``__call__`` interface as pytest-benchmark's
    ``benchmark`` fixture so tests work identically in both modes.
    """

    def __init__(self) -> None:
        self.times_ms: list[float] = []
        self.mean_ms: float = 0.0

    def __call__(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        # Warmup
        result: Any = None
        for _ in range(_WARMUP_ROUNDS):
            result = fn(*args, **kwargs)

        # Measure
        self.times_ms.clear()
        for _ in range(_MEASURE_ROUNDS):
            t0 = time.perf_counter()
            result = fn(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - t0) * 1_000
            self.times_ms.append(elapsed_ms)

        self.mean_ms = sum(self.times_ms) / len(self.times_ms)
        return result


def _get_benchmark(request: pytest.FixtureRequest) -> Any:
    """Return pytest-benchmark fixture if available, else fallback."""
    try:
        return request.getfixturevalue("benchmark")
    except pytest.FixtureLookupError:
        return _FallbackBenchmark()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def boot_context() -> Any:
    """Provide a fresh _BootContext for each test."""
    return _make_boot_context()


@pytest.fixture
def bench(request: pytest.FixtureRequest) -> Any:
    """Provide benchmark runner (pytest-benchmark or fallback)."""
    return _get_benchmark(request)


# ---------------------------------------------------------------------------
# Tests — full factory boot
# ---------------------------------------------------------------------------


class TestFullFactoryBoot:
    """End-to-end factory boot with mocked I/O."""

    def test_create_nexus_services_under_budget(self, bench: Any) -> None:
        """Full create_nexus_services() must complete under budget."""
        from nexus.factory import create_nexus_services

        record_store = _make_mock_record_store()
        metadata_store = _make_mock_metadata_store()
        backend = _make_mock_backend()
        dlc = _make_mock_dlc()

        def boot() -> Any:
            return create_nexus_services(
                record_store=record_store,
                metadata_store=metadata_store,
                backend=backend,
                dlc=dlc,
                enable_write_buffer=False,
            )

        result = bench(boot)

        # Verify a single flat dict was returned
        assert isinstance(result, dict)
        assert "rebac_manager" in result
        assert "permission_enforcer" in result

        # Assert timing budget
        if isinstance(bench, _FallbackBenchmark):
            assert bench.mean_ms < _BOOT_TIME_BUDGET_MS, (
                f"Factory boot took {bench.mean_ms:.1f}ms (budget: {_BOOT_TIME_BUDGET_MS}ms)"
            )
            print(
                f"\n  [FACTORY BOOT] mean={bench.mean_ms:.1f}ms "
                f"(budget={_BOOT_TIME_BUDGET_MS}ms, "
                f"rounds={_MEASURE_ROUNDS})"
            )

    def test_boot_time_hard_assert(self) -> None:
        """Hard wall-clock assertion — always runs, no benchmark fixture."""
        from nexus.factory import create_nexus_services

        record_store = _make_mock_record_store()
        metadata_store = _make_mock_metadata_store()
        backend = _make_mock_backend()
        dlc = _make_mock_dlc()

        # Warmup
        for _ in range(_WARMUP_ROUNDS):
            create_nexus_services(
                record_store=record_store,
                metadata_store=metadata_store,
                backend=backend,
                dlc=dlc,
                enable_write_buffer=False,
            )

        # Measure
        times_ms: list[float] = []
        for _ in range(_MEASURE_ROUNDS):
            t0 = time.perf_counter()
            create_nexus_services(
                record_store=record_store,
                metadata_store=metadata_store,
                backend=backend,
                dlc=dlc,
                enable_write_buffer=False,
            )
            times_ms.append((time.perf_counter() - t0) * 1_000)

        mean_ms = sum(times_ms) / len(times_ms)
        min_ms = min(times_ms)
        max_ms = max(times_ms)

        print(f"\n  [HARD ASSERT] mean={mean_ms:.1f}ms min={min_ms:.1f}ms max={max_ms:.1f}ms")
        assert mean_ms < _BOOT_TIME_BUDGET_MS, (
            f"Factory boot took {mean_ms:.1f}ms (budget: {_BOOT_TIME_BUDGET_MS}ms)"
        )


# ---------------------------------------------------------------------------
# Tests — per-tier breakdown
# ---------------------------------------------------------------------------


class TestPerTierBreakdown:
    """Measure each boot tier independently for regression tracking."""

    def test_kernel_tier(self, boot_context: Any, bench: Any) -> None:
        """Tier 0 (KERNEL) validation time — returns empty dict."""
        from nexus.factory import _boot_kernel_services

        ctx = boot_context

        def boot_kernel() -> dict[str, Any]:
            return _boot_kernel_services(ctx)

        result = bench(boot_kernel)

        # Issue #2193: Kernel returns empty dict (validation only)
        assert isinstance(result, dict)
        assert len(result) == 0

        if isinstance(bench, _FallbackBenchmark):
            print(f"\n  [KERNEL] mean={bench.mean_ms:.1f}ms")

    def test_system_tier(self, boot_context: Any, bench: Any) -> None:
        """Tier 1 (SYSTEM) construction time — includes former-kernel services."""
        from nexus.factory import _boot_system_services

        ctx = boot_context

        def boot_system() -> dict[str, Any]:
            return _boot_system_services(ctx)

        result = bench(boot_system)

        assert isinstance(result, dict)
        # Issue #2193: System now includes former-kernel services
        expected_keys = {
            # Former-kernel critical
            "rebac_manager",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "write_observer",
            # Former-kernel degradable
            "dir_visibility_cache",
            "hierarchy_manager",
            "deferred_permission_buffer",
            "workspace_registry",
            "mount_manager",
            "workspace_manager",
            # Original system services
            "eviction_manager",
            "namespace_manager",
            "async_namespace_manager",
            "async_vfs_router",
            "delivery_worker",
            "observability_subsystem",
            "resiliency_manager",
            "context_branch_service",
        }
        assert set(result.keys()) == expected_keys, (
            f"System tier key mismatch. "
            f"Missing: {expected_keys - set(result.keys())}. "
            f"Extra: {set(result.keys()) - expected_keys}."
        )

        if isinstance(bench, _FallbackBenchmark):
            print(f"\n  [SYSTEM] mean={bench.mean_ms:.1f}ms")

    def test_brick_tier(self, boot_context: Any, bench: Any) -> None:
        """Tier 2 (BRICK) construction time."""
        from nexus.factory import _boot_brick_services, _boot_system_services

        ctx = boot_context
        system = _boot_system_services(ctx)

        def boot_brick() -> dict[str, Any]:
            return _boot_brick_services(ctx, system)

        result = bench(boot_brick)

        assert isinstance(result, dict)
        expected_keys = {
            "agent_event_log",
            "wallet_provisioner",
            "manifest_resolver",
            "manifest_metrics",
            "tool_namespace_middleware",
            "chunked_upload_service",
            "event_bus",
            "lock_manager",
            "workflow_engine",
            "api_key_creator",
            "snapshot_service",
            "skill_service",
            "skill_package_service",
            "delegation_service",
            "version_service",
            "rebac_circuit_breaker",
            "memory_router",
            "memory_permission",
        }
        assert set(result.keys()) == expected_keys, (
            f"Brick tier key mismatch. "
            f"Missing: {expected_keys - set(result.keys())}. "
            f"Extra: {set(result.keys()) - expected_keys}."
        )

        if isinstance(bench, _FallbackBenchmark):
            print(f"\n  [BRICK] mean={bench.mean_ms:.1f}ms")

    def test_all_tiers_sum_under_budget(self, boot_context: Any) -> None:
        """Sum of individual tier times must stay under the total budget."""
        from nexus.factory import (
            _boot_brick_services,
            _boot_kernel_services,
            _boot_system_services,
        )

        ctx = boot_context

        # Warmup
        _boot_kernel_services(ctx)
        system = _boot_system_services(ctx)
        _boot_brick_services(ctx, system)

        # Measure each tier
        tier_times: dict[str, float] = {}

        t0 = time.perf_counter()
        _boot_kernel_services(ctx)
        tier_times["kernel"] = (time.perf_counter() - t0) * 1_000

        t0 = time.perf_counter()
        system = _boot_system_services(ctx)
        tier_times["system"] = (time.perf_counter() - t0) * 1_000

        t0 = time.perf_counter()
        _boot_brick_services(ctx, system)
        tier_times["brick"] = (time.perf_counter() - t0) * 1_000

        total_ms = sum(tier_times.values())

        print("\n  [TIER BREAKDOWN]")
        for tier, ms in tier_times.items():
            pct = (ms / total_ms * 100) if total_ms > 0 else 0
            print(f"    {tier:>8s}: {ms:6.1f}ms ({pct:4.1f}%)")
        print(f"    {'total':>8s}: {total_ms:6.1f}ms (budget: {_BOOT_TIME_BUDGET_MS}ms)")

        assert total_ms < _BOOT_TIME_BUDGET_MS, (
            f"Tier sum {total_ms:.1f}ms exceeds budget {_BOOT_TIME_BUDGET_MS}ms. "
            f"Breakdown: {tier_times}"
        )
