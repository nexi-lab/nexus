"""Tests for AdapterRegistry and CircuitBreaker.

Covers:
  - CircuitBreaker state transitions (closed, tripped, half-open, reset)
  - AdapterRegistry.startup() with concurrency and global timeout
  - Per-adapter circuit breaker integration
  - Background refresh loop with TTL
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.external_sync.registry import AdapterRegistry, CircuitBreaker
from nexus.bricks.auth.profile import InMemoryAuthProfileStore

# ---------------------------------------------------------------------------
# Helper adapters
# ---------------------------------------------------------------------------


class _FastAdapter(ExternalCliSyncAdapter):
    """Returns 1 profile immediately."""

    adapter_name = "fast"
    sync_ttl_seconds = 60.0
    failure_threshold = 3
    reset_timeout_seconds = 60.0

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=[
                SyncedProfile(
                    provider="test",
                    account_identifier="fast-acct",
                    backend_key="fast/fast-acct",
                    source="fast",
                ),
            ],
        )

    async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="fast-key")


class _HangingAdapter(ExternalCliSyncAdapter):
    """Sleeps 300s — always times out."""

    adapter_name = "hanging"
    sync_ttl_seconds = 60.0
    failure_threshold = 3
    reset_timeout_seconds = 60.0

    async def detect(self) -> bool:
        await asyncio.sleep(300)
        return True

    async def sync(self) -> SyncResult:
        await asyncio.sleep(300)
        return SyncResult(adapter_name=self.adapter_name)

    async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="never")


class _FailingAdapter(ExternalCliSyncAdapter):
    """Always returns error, tracks sync_count."""

    adapter_name = "failing"
    sync_ttl_seconds = 0.1  # short TTL for testing
    failure_threshold = 3
    reset_timeout_seconds = 0.5

    def __init__(self) -> None:
        self.sync_count = 0

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        self.sync_count += 1
        return SyncResult(adapter_name=self.adapter_name, error="always fails")

    async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
        raise RuntimeError("always fails")


class _RecoveringAdapter(ExternalCliSyncAdapter):
    """Fails N times then succeeds."""

    adapter_name = "recovering"
    sync_ttl_seconds = 0.1
    failure_threshold = 2
    reset_timeout_seconds = 0.3

    def __init__(self, *, fail_times: int = 2) -> None:
        self._fail_times = fail_times
        self.sync_count = 0

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        self.sync_count += 1
        if self.sync_count <= self._fail_times:
            return SyncResult(adapter_name=self.adapter_name, error="not yet")
        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=[
                SyncedProfile(
                    provider="test",
                    account_identifier="recovered-acct",
                    backend_key="recovering/recovered-acct",
                    source="recovering",
                ),
            ],
        )

    async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="api_key", api_key="recovered")


# ---------------------------------------------------------------------------
# TestCircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=1.0)
        assert not cb.is_tripped
        assert not cb.is_half_open
        assert cb.failure_count == 0

    def test_trips_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_tripped
        cb.record_failure()
        assert cb.is_tripped
        assert not cb.is_half_open

    def test_success_resets(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_tripped
        cb.record_success()
        assert not cb.is_tripped
        assert not cb.is_half_open
        assert cb.failure_count == 0

    def test_half_open_after_reset_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.1)
        cb.record_failure()
        assert cb.is_tripped
        time.sleep(0.15)
        assert not cb.is_tripped
        assert cb.is_half_open


# ---------------------------------------------------------------------------
# TestRegistryStartup
# ---------------------------------------------------------------------------


class TestRegistryStartup:
    async def test_returns_results(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [_FastAdapter()],
            store,
            startup_timeout=3.0,
        )
        results = await registry.startup()
        assert "fast" in results
        assert results["fast"].error is None
        assert len(results["fast"].profiles) == 1

    async def test_upserts_into_store(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [_FastAdapter()],
            store,
            startup_timeout=3.0,
        )
        await registry.startup()

        profiles = store.list()
        assert len(profiles) == 1
        p = profiles[0]
        assert p.backend == "external-cli"
        assert p.backend_key == "fast/fast-acct"
        assert p.provider == "test"
        assert p.last_synced_at is not None

    async def test_timeout_degrades_slow_adapter(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [_HangingAdapter()],
            store,
            startup_timeout=0.5,
        )
        results = await registry.startup()
        assert results["hanging"].error == "timeout"
        assert store.list() == []

    async def test_5_adapters_4_fast_1_hanging(self) -> None:
        """Four fast adapters + one hanging. All fast should succeed in < 2s."""

        class _NamedFastAdapter(_FastAdapter):
            """Fast adapter with configurable name for unique profiles."""

            def __init__(self, name: str) -> None:
                self.adapter_name = name
                self._name = name

            async def sync(self) -> SyncResult:
                return SyncResult(
                    adapter_name=self._name,
                    profiles=[
                        SyncedProfile(
                            provider="test",
                            account_identifier=f"{self._name}-acct",
                            backend_key=f"{self._name}/{self._name}-acct",
                            source=self._name,
                        ),
                    ],
                )

        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [
                _NamedFastAdapter("fast1"),
                _NamedFastAdapter("fast2"),
                _NamedFastAdapter("fast3"),
                _NamedFastAdapter("fast4"),
                _HangingAdapter(),
            ],
            store,
            startup_timeout=2.0,
        )
        t0 = time.monotonic()
        results = await registry.startup()
        elapsed = time.monotonic() - t0

        assert elapsed < 2.5  # generous margin but well under 300s
        for name in ("fast1", "fast2", "fast3", "fast4"):
            assert results[name].error is None
        assert results["hanging"].error == "timeout"
        assert len(store.list()) == 4


# ---------------------------------------------------------------------------
# TestRegistryCircuitBreaker
# ---------------------------------------------------------------------------


class TestRegistryCircuitBreaker:
    async def test_trips_after_threshold(self) -> None:
        adapter = _FailingAdapter()
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [adapter],
            store,
            startup_timeout=3.0,
            loop_tick_seconds=0.05,
        )
        # startup counts as first failure
        await registry.startup()

        breaker = registry._breakers["failing"]
        assert breaker.failure_count == 1

        # Two more syncs to trip the breaker (threshold=3)
        await registry._sync_adapter(adapter)
        assert breaker.failure_count == 2
        await registry._sync_adapter(adapter)
        assert breaker.failure_count == 3
        assert breaker.is_tripped

    async def test_half_open_recovery(self) -> None:
        """After breaker trips and reset timeout elapses, a successful probe resets it."""
        adapter = _RecoveringAdapter(fail_times=2)
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [adapter],
            store,
            startup_timeout=3.0,
            loop_tick_seconds=0.05,
        )

        # Two failures trip the breaker (threshold=2)
        await registry._sync_adapter(adapter)
        await registry._sync_adapter(adapter)
        breaker = registry._breakers["recovering"]
        assert breaker.is_tripped

        # Wait for reset timeout (0.3s)
        await asyncio.sleep(0.4)
        assert breaker.is_half_open

        # Next sync succeeds (sync_count=3 > fail_times=2)
        await registry._sync_adapter(adapter)
        assert not breaker.is_tripped
        assert not breaker.is_half_open
        assert breaker.failure_count == 0

        # Check profile was upserted
        profiles = store.list()
        assert len(profiles) == 1
        assert profiles[0].backend == "external-cli"


# ---------------------------------------------------------------------------
# TestRegistryRefreshLoop
# ---------------------------------------------------------------------------


class TestRegistryRefreshLoop:
    async def test_respects_ttl(self) -> None:
        """Short TTL (0.3s) with fast tick (0.1s) — adapter syncs multiple times."""

        class _CountingAdapter(_FastAdapter):
            adapter_name = "counting"
            sync_ttl_seconds = 0.3

            def __init__(self) -> None:
                self.sync_count = 0

            async def sync(self) -> SyncResult:
                self.sync_count += 1
                return await super().sync()

        adapter = _CountingAdapter()
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [adapter],
            store,
            startup_timeout=3.0,
            loop_tick_seconds=0.1,
        )

        # Startup does the first sync
        await registry.startup()
        assert adapter.sync_count == 1

        # Run refresh loop for ~0.8s, then cancel
        loop_task = asyncio.create_task(registry.run_refresh_loop())
        await asyncio.sleep(0.8)
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

        # With TTL=0.3s and tick=0.1s over ~0.8s, we expect at least 2 additional syncs
        assert adapter.sync_count >= 3
