"""Tests for AdapterRegistry and CircuitBreaker.

Covers:
  - CircuitBreaker state transitions (closed, tripped, half-open, reset)
  - AdapterRegistry.startup() with concurrency and global timeout
  - Per-adapter circuit breaker integration
  - Background refresh loop with TTL
  - Concurrency safety (torn reads, crash-freedom)
  - Offline safety (FileAdapter / SubprocessAdapter degrade gracefully)
"""

from __future__ import annotations

import asyncio
import socket as _socket_mod
import time
from pathlib import Path

import pytest

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter
from nexus.bricks.auth.external_sync.registry import AdapterRegistry, CircuitBreaker
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter
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


# ---------------------------------------------------------------------------
# TestConcurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_list_during_upsert(self) -> None:
        """Two readers + one writer — no torn reads, no crashes."""

        class _ShortTTLAdapter(_FastAdapter):
            adapter_name = "short-ttl"
            sync_ttl_seconds = 0.1

        adapter = _ShortTTLAdapter()
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry(
            [adapter],
            store,
            startup_timeout=3.0,
            loop_tick_seconds=0.05,
        )
        await registry.startup()

        read_results: list[list] = [[], []]

        async def reader(idx: int) -> None:
            for _ in range(20):
                result = store.list(provider="test")
                read_results[idx].append(result)
                await asyncio.sleep(0.01)

        async def writer() -> None:
            loop_task = asyncio.create_task(registry.run_refresh_loop())
            await asyncio.sleep(0.3)
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

        # Run 2 readers + 1 writer concurrently
        await asyncio.gather(reader(0), reader(1), writer())

        # Assert: no exceptions were raised, and all read results are lists
        for idx in range(2):
            assert len(read_results[idx]) == 20
            for result in read_results[idx]:
                assert isinstance(result, list)


# ---------------------------------------------------------------------------
# TestOfflineSafety
# ---------------------------------------------------------------------------


class TestOfflineSafety:
    """Verify adapters degrade gracefully when network is blocked.

    These tests are sync because the ``no_network`` fixture monkeypatches
    ``socket.socket``, which also blocks ``socket.socketpair()`` — the call
    asyncio uses internally for its event-loop self-pipe.  We therefore
    create the event loop *first*, block sockets *second*, then run the
    adapter coroutines inside the pre-existing loop.
    """

    def test_file_adapter_returns_degraded_with_no_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FileAdapter with nonexistent file returns error, no hang."""

        class _MissingFileAdapter(FileAdapter):
            adapter_name = "missing-file"

            def paths(self) -> list[Path]:
                return [Path("/tmp/__nexus_test_nonexistent_config_1234567890__")]

            def parse_file(self, _path: Path, _content: str) -> list[SyncedProfile]:
                return []

            async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="never")

        adapter = _MissingFileAdapter()

        # Create the event loop BEFORE blocking sockets, then block.
        loop = asyncio.new_event_loop()
        try:
            monkeypatch.setattr(
                _socket_mod,
                "socket",
                lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("network blocked")),
            )
            result = loop.run_until_complete(asyncio.wait_for(adapter.sync(), timeout=2.0))
        finally:
            loop.close()
        assert result.error is not None

    def test_subprocess_adapter_returns_degraded_with_no_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SubprocessAdapter with nonexistent binary returns error, no hang."""

        class _MissingBinaryAdapter(SubprocessAdapter):
            adapter_name = "missing-binary"
            binary_name = "__nexus_test_nonexistent_binary_1234567890__"

            def get_status_args(self) -> tuple[str, ...]:
                return ("--version",)

            def parse_output(self, _stdout: str, _stderr: str) -> list[SyncedProfile]:
                return []

            async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="never")

        adapter = _MissingBinaryAdapter()

        # Create the event loop BEFORE blocking sockets, then block.
        loop = asyncio.new_event_loop()
        try:
            monkeypatch.setattr(
                _socket_mod,
                "socket",
                lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("network blocked")),
            )
            result = loop.run_until_complete(asyncio.wait_for(adapter.sync(), timeout=2.0))
        finally:
            loop.close()
        assert result.error is not None


class TestTombstoning:
    """R3-H3 regression: stale adapter-owned profiles must be deleted on
    successful resync. Otherwise logout/account-switch leaves ghost rows
    that auth selection still treats as usable.
    """

    async def test_resync_removes_vanished_profiles(self) -> None:
        store = InMemoryAuthProfileStore()

        # Stateful adapter: returns alice on first sync, bob on second.
        class _SwitchingAdapter(ExternalCliSyncAdapter):
            adapter_name = "switch"
            sync_ttl_seconds = 0.1
            failure_threshold = 3
            reset_timeout_seconds = 60.0

            def __init__(self) -> None:
                self.call = 0

            async def detect(self) -> bool:
                return True

            async def sync(self) -> SyncResult:
                self.call += 1
                email = "alice@example.com" if self.call == 1 else "bob@example.com"
                return SyncResult(
                    adapter_name=self.adapter_name,
                    profiles=[
                        SyncedProfile(
                            provider="test",
                            account_identifier=email,
                            backend_key=f"switch/{email}",
                            source="switch",
                        ),
                    ],
                )

            async def resolve_credential(self, _bk: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="x")

        adapter = _SwitchingAdapter()
        registry = AdapterRegistry([adapter], store, startup_timeout=3.0)

        # First sync: alice present
        await registry.startup()
        ids_after_first = {p.id for p in store.list()}
        assert "test/alice@example.com" in ids_after_first
        assert "test/bob@example.com" not in ids_after_first

        # Second sync (force via internal helper): bob present, alice gone
        await registry._sync_adapter(adapter)
        ids_after_second = {p.id for p in store.list()}
        assert "test/bob@example.com" in ids_after_second
        assert "test/alice@example.com" not in ids_after_second, (
            "alice should be tombstoned — she vanished from the source CLI"
        )

    async def test_degraded_sync_does_not_tombstone(self) -> None:
        """If sync returns error (transient parse error etc), do NOT delete
        existing rows — that would wipe valid profiles on flaky CLIs."""
        store = InMemoryAuthProfileStore()

        class _FlippyAdapter(ExternalCliSyncAdapter):
            adapter_name = "flippy"
            sync_ttl_seconds = 0.1
            failure_threshold = 3
            reset_timeout_seconds = 60.0

            def __init__(self) -> None:
                self.call = 0

            async def detect(self) -> bool:
                return True

            async def sync(self) -> SyncResult:
                self.call += 1
                if self.call == 1:
                    return SyncResult(
                        adapter_name=self.adapter_name,
                        profiles=[
                            SyncedProfile(
                                provider="test",
                                account_identifier="alice@example.com",
                                backend_key="flippy/alice@example.com",
                                source="flippy",
                            ),
                        ],
                    )
                # Second sync: degraded with no profiles
                return SyncResult(adapter_name=self.adapter_name, error="parse error", profiles=[])

            async def resolve_credential(self, _bk: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="x")

        adapter = _FlippyAdapter()
        registry = AdapterRegistry([adapter], store, startup_timeout=3.0)
        await registry.startup()
        assert any(p.id == "test/alice@example.com" for p in store.list())

        await registry._sync_adapter(adapter)
        # Alice survives the degraded sync
        assert any(p.id == "test/alice@example.com" for p in store.list()), (
            "degraded sync (error != None) must NOT tombstone existing profiles"
        )

    async def test_tombstone_only_touches_owned_rows(self) -> None:
        """Adapter A re-syncing must NOT delete adapter B's rows."""
        store = InMemoryAuthProfileStore()

        # Pre-populate with an adapter-B-owned row
        from datetime import UTC
        from datetime import datetime as _dt

        from nexus.bricks.auth.profile import (
            AuthProfile as _AP,
        )
        from nexus.bricks.auth.profile import (
            ProfileUsageStats as _PS,
        )

        store.upsert(
            _AP(
                id="other/bob@example.com",
                provider="other",
                account_identifier="bob@example.com",
                backend="external-cli",
                backend_key="other-adapter/bob@example.com",
                last_synced_at=_dt.now(UTC),
                sync_ttl_seconds=300,
                usage_stats=_PS(),
            )
        )

        # Adapter A syncs, owns no rows in store yet
        adapter = _FastAdapter()  # adapter_name="fast", produces fast/fast-acct
        registry = AdapterRegistry([adapter], store, startup_timeout=3.0)
        await registry.startup()

        ids = {p.id for p in store.list()}
        # Both should survive: A added its own row, B's row untouched
        assert "test/fast-acct" in ids
        assert "other/bob@example.com" in ids, "A's sync must not delete B's adapter-owned row"

    async def test_failed_atomic_swap_does_not_record_success(self) -> None:
        """R6-H3 regression: when ``replace_owned_subset`` raises, the
        registry must NOT stamp ``_last_sync_times`` or record breaker
        success. Otherwise the next refresh tick skips the retry — the
        sync silently appears done while the store still holds the
        pre-sync snapshot.
        """

        class _FailingStore(InMemoryAuthProfileStore):
            def replace_owned_subset(self, *, upserts, deletes) -> None:  # noqa: ANN001, ARG002
                raise RuntimeError("simulated SQLite busy timeout")

        store = _FailingStore()
        adapter = _FastAdapter()
        registry = AdapterRegistry([adapter], store, startup_timeout=3.0)

        await registry.startup()
        # Store apply failed → no last_sync_times entry, breaker counted failure
        assert "fast" not in registry._last_sync_times, (
            "must not stamp last_sync_times when store apply failed"
        )
        breaker = registry._breakers["fast"]
        assert breaker.failure_count == 1, "failed store apply must increment breaker.failure_count"

        # _sync_adapter path uses the same gating
        await registry._sync_adapter(adapter)
        assert "fast" not in registry._last_sync_times
        assert breaker.failure_count == 2

    async def test_resync_uses_atomic_replace_owned_subset(self) -> None:
        """R4-MEDIUM regression: registry must batch upserts+tombstones via
        ``store.replace_owned_subset`` so concurrent readers never see a
        half-applied snapshot. Previously the registry called ``upsert`` in a
        loop then ``delete`` in a second loop — each individually committed —
        opening a window where a fresh row coexisted with about-to-vanish
        stale rows.
        """

        # Custom store that records call order so we can assert atomicity.
        class _RecordingStore(InMemoryAuthProfileStore):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[tuple[str, tuple[int, int]]] = []

            def upsert(self, profile, *, preserve_runtime_state: bool = False) -> None:  # noqa: ANN001
                self.calls.append(("upsert", (1, 0)))
                super().upsert(profile, preserve_runtime_state=preserve_runtime_state)

            def delete(self, pid: str) -> None:
                self.calls.append(("delete", (0, 1)))
                super().delete(pid)

            def replace_owned_subset(self, *, upserts, deletes) -> None:  # noqa: ANN001
                self.calls.append(("replace_owned_subset", (len(upserts), len(deletes))))
                super().replace_owned_subset(upserts=upserts, deletes=deletes)

        store = _RecordingStore()

        class _SwitchingAdapter(ExternalCliSyncAdapter):
            adapter_name = "atomic"
            sync_ttl_seconds = 0.1
            failure_threshold = 3
            reset_timeout_seconds = 60.0

            def __init__(self) -> None:
                self.call = 0

            async def detect(self) -> bool:
                return True

            async def sync(self) -> SyncResult:
                self.call += 1
                email = "alice@example.com" if self.call == 1 else "bob@example.com"
                return SyncResult(
                    adapter_name=self.adapter_name,
                    profiles=[
                        SyncedProfile(
                            provider="test",
                            account_identifier=email,
                            backend_key=f"atomic/{email}",
                            source="atomic",
                        ),
                    ],
                )

            async def resolve_credential(self, _bk: str) -> ResolvedCredential:
                return ResolvedCredential(kind="api_key", api_key="x")

        adapter = _SwitchingAdapter()
        registry = AdapterRegistry([adapter], store, startup_timeout=3.0)

        # First sync writes alice — exactly one batched call, no per-row writes.
        await registry.startup()
        first_calls = [c[0] for c in store.calls]
        assert first_calls == ["replace_owned_subset"], (
            f"first sync must use atomic batch, got {first_calls}"
        )

        # Second sync: bob in, alice tombstoned — still one atomic call.
        store.calls.clear()
        await registry._sync_adapter(adapter)
        second_calls = list(store.calls)
        assert len(second_calls) == 1, (
            f"second sync must use exactly one atomic call, got {second_calls}"
        )
        assert second_calls[0][0] == "replace_owned_subset"
        # 1 upsert (bob), 1 delete (alice)
        assert second_calls[0][1] == (1, 1)
