"""TigerCacheManager boot-path tests (Issue #4342).

nexusd boot hung indefinitely because ``initialize()`` runs
``sync_resource_map()`` synchronously on the boot thread with no deadline:
a wedged metastore/DB call blocks instead of raising, so the fail-soft
try/except never fires and the port is never bound. These tests pin the
bounded/fail-soft contract: boot must proceed even when the sync hangs.
"""

import threading
import time

import pytest

from nexus.bricks.rebac.tiger_cache_manager import TigerCacheManager


class FakeResourceMap:
    """Records get_or_create_int_id calls; thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def get_or_create_int_id(self, resource_type: str, resource_id: str) -> int:
        with self._lock:
            self.calls.append((resource_type, resource_id))
            return len(self.calls)

    def paths(self) -> list[str]:
        with self._lock:
            return [rid for _, rid in self.calls]


class FakeTigerCache:
    def __init__(self, resource_map: FakeResourceMap) -> None:
        self._resource_map = resource_map


class FakeRebacManager:
    def __init__(self, resource_map: FakeResourceMap) -> None:
        self._tiger_cache = FakeTigerCache(resource_map)


class FakeNexusFS:
    """sys_readdir yields ``before`` entries, then blocks on ``gate`` (if
    given), then yields ``after`` entries — emulating a wedged metastore
    listing mid-iteration."""

    def __init__(
        self,
        before: list[str],
        gate: threading.Event | None = None,
        after: list[str] | None = None,
    ) -> None:
        self._before = before
        self._gate = gate
        self._after = after or []

    def sys_readdir(self, path, recursive=False, details=False, context=None):
        yield from self._before
        if self._gate is not None:
            self._gate.wait()
        yield from self._after


def make_manager(nexus_fs: FakeNexusFS, resource_map: FakeResourceMap) -> TigerCacheManager:
    return TigerCacheManager(
        rebac_manager=FakeRebacManager(resource_map),
        nexus_fs=nexus_fs,
        default_zone_id="root",
    )


def test_initialize_returns_when_readdir_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot must not block indefinitely on a wedged resource-map sync."""
    monkeypatch.setenv("NEXUS_TIGER_INIT_TIMEOUT_SECONDS", "0.2")
    gate = threading.Event()  # never set before the assertion: sync is wedged
    resource_map = FakeResourceMap()
    manager = make_manager(FakeNexusFS(["/a.txt"], gate=gate), resource_map)

    boot = threading.Thread(target=manager.initialize, daemon=True)
    start = time.monotonic()
    boot.start()
    boot.join(timeout=5.0)
    elapsed = time.monotonic() - start

    try:
        assert not boot.is_alive(), (
            f"initialize() still blocked after {elapsed:.1f}s with a wedged "
            "sys_readdir; boot must proceed (Issue #4342)"
        )
    finally:
        gate.set()  # let the background sync thread finish


def test_initialize_completes_sync_before_returning_when_fast() -> None:
    """Healthy case keeps today's semantics: map is populated synchronously."""
    resource_map = FakeResourceMap()
    manager = make_manager(FakeNexusFS(["/a.txt", "/b/c.txt"]), resource_map)

    manager.initialize()

    assert resource_map.paths() == ["/a.txt", "/b/c.txt"]
    assert all(rtype == "file" for rtype, _ in resource_map.calls)


def test_sync_finishes_in_background_after_boot_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync that outlives the boot timeout still completes in background."""
    monkeypatch.setenv("NEXUS_TIGER_INIT_TIMEOUT_SECONDS", "0.1")
    gate = threading.Event()
    resource_map = FakeResourceMap()
    manager = make_manager(FakeNexusFS(["/a.txt"], gate=gate, after=["/b.txt"]), resource_map)

    manager.initialize()  # returns after ~0.1s, sync wedged on gate
    assert "/b.txt" not in resource_map.paths()

    gate.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if "/b.txt" in resource_map.paths():
            break
        time.sleep(0.01)
    assert "/b.txt" in resource_map.paths(), "background sync never completed"


def test_disable_env_skips_sync_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_DISABLE_PERF_OPTIMIZATIONS", "true")
    resource_map = FakeResourceMap()
    manager = make_manager(FakeNexusFS(["/a.txt"]), resource_map)

    manager.initialize()

    assert resource_map.calls == []


class EndlessNexusFS:
    """sys_readdir yields entries forever — a grinding, never-finishing sync."""

    def sys_readdir(self, path, recursive=False, details=False, context=None):
        i = 0
        while True:
            yield f"/grind/{i}.txt"
            i += 1
            time.sleep(0.005)


def test_stop_worker_halts_in_progress_background_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TIGER_INIT_TIMEOUT_SECONDS", "0.05")
    resource_map = FakeResourceMap()
    manager = TigerCacheManager(
        rebac_manager=FakeRebacManager(resource_map),
        nexus_fs=EndlessNexusFS(),
        default_zone_id="root",
    )

    manager.initialize()  # returns after timeout; sync grinds in background
    sync_thread = manager._sync_thread
    assert sync_thread is not None and sync_thread.is_alive()

    manager.stop_worker()
    sync_thread.join(timeout=5.0)
    assert not sync_thread.is_alive(), "stop_worker() must end the sync loop"


def test_initialize_is_single_flight_while_sync_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-initialize while a sync is still running must not spawn a second
    sync thread (orphaning the first) nor clear its stop event."""
    monkeypatch.setenv("NEXUS_TIGER_INIT_TIMEOUT_SECONDS", "0.05")
    resource_map = FakeResourceMap()
    manager = TigerCacheManager(
        rebac_manager=FakeRebacManager(resource_map),
        nexus_fs=EndlessNexusFS(),
        default_zone_id="root",
    )

    manager.initialize()  # returns after timeout; sync grinds in background
    first_thread = manager._sync_thread
    assert first_thread is not None and first_thread.is_alive()

    manager.initialize()  # manual refresh while sync still running
    assert manager._sync_thread is first_thread, (
        "re-initialize while a sync is alive must be single-flight"
    )

    manager.stop_worker()
    first_thread.join(timeout=5.0)
    assert not first_thread.is_alive()


def test_blocked_materialized_listing_still_bounds_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production sys_readdir returns a fully materialized list — a wedged
    listing blocks inside the call itself. Boot must still be bounded."""
    monkeypatch.setenv("NEXUS_TIGER_INIT_TIMEOUT_SECONDS", "0.2")
    gate = threading.Event()

    class BlockingListNexusFS:
        def sys_readdir(self, path, recursive=False, details=False, context=None):
            gate.wait()  # block before returning, like a wedged metastore
            return ["/a.txt"]  # materialized list, not a generator

    resource_map = FakeResourceMap()
    manager = TigerCacheManager(
        rebac_manager=FakeRebacManager(resource_map),
        nexus_fs=BlockingListNexusFS(),
        default_zone_id="root",
    )

    boot = threading.Thread(target=manager.initialize, daemon=True)
    boot.start()
    boot.join(timeout=5.0)
    try:
        assert not boot.is_alive(), "boot must proceed while the listing is wedged"
    finally:
        gate.set()


def test_stop_worker_is_safe_without_initialize_and_idempotent() -> None:
    """stop_worker() is wired as a NexusFS close callback (Issue #4342), so it
    must be a no-op when initialize() never ran and safe to call twice."""
    resource_map = FakeResourceMap()
    manager = make_manager(FakeNexusFS(["/a.txt"]), resource_map)

    manager.stop_worker()  # never initialized — must not raise

    manager.initialize()
    manager.stop_worker()
    manager.stop_worker()  # second call must not raise
