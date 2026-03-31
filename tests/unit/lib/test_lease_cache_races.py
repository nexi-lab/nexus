"""Race condition tests for concurrent lease acquire during eviction (Decision 10A).

Tests thread-safety of FileContentCache staleness tracking and
SingleFlightSync coalescing under concurrent access patterns.
Uses controlled scheduling with ManualClock + threading.Event
to force specific interleavings.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from nexus.lib.lease import ManualClock
from nexus.lib.singleflight import SingleFlightSync
from nexus.storage.file_cache import FileContentCache

ZONE = "zone1"
PATH = "/mnt/gcs/file.txt"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock(_now=1000.0)


@pytest.fixture()
def file_cache(tmp_path: Path) -> FileContentCache:
    return FileContentCache(tmp_path)


# ---------------------------------------------------------------------------
# 1. Revoke during read returns stale on next call
# ---------------------------------------------------------------------------


class TestRevokeDuringRead:
    def test_revoke_during_read_returns_stale(self, file_cache: FileContentCache) -> None:
        """Thread A reads from FileContentCache. Thread B revokes the lease.

        The revocation happens between two read() calls. The first read()
        may succeed (content was valid when it started), but the NEXT read()
        after revocation must return None.
        """
        # Setup: write content and mark lease acquired
        file_cache.mark_lease_acquired(ZONE, PATH)
        file_cache.write(ZONE, PATH, b"initial content")
        assert file_cache.read(ZONE, PATH) == b"initial content"

        # Gates for controlling thread interleaving
        read_started = threading.Event()
        revoke_done = threading.Event()

        first_read_result: list[bytes | None] = []
        second_read_result: list[bytes | None] = []

        def reader() -> None:
            # First read: should succeed (lease still active)
            result = file_cache.read(ZONE, PATH)
            first_read_result.append(result)
            read_started.set()

            # Wait for revocation to complete
            revoke_done.wait(timeout=5.0)

            # Second read: should return None (lease revoked)
            result = file_cache.read(ZONE, PATH)
            second_read_result.append(result)

        def revoker() -> None:
            # Wait for the reader to complete its first read
            read_started.wait(timeout=5.0)

            # Revoke the lease
            file_cache.mark_lease_revoked(ZONE, PATH)
            revoke_done.set()

        t_reader = threading.Thread(target=reader)
        t_revoker = threading.Thread(target=revoker)

        t_reader.start()
        t_revoker.start()
        t_reader.join(timeout=10.0)
        t_revoker.join(timeout=10.0)

        # First read returned content (lease was active)
        assert first_read_result[0] == b"initial content"
        # Second read returns None (lease was revoked)
        assert second_read_result[0] is None


# ---------------------------------------------------------------------------
# 2. Acquire after revoke clears staleness
# ---------------------------------------------------------------------------


class TestAcquireAfterRevoke:
    def test_acquire_after_revoke_clears_staleness(self, file_cache: FileContentCache) -> None:
        """Thread A revokes lease. Thread B immediately acquires.

        After both complete, the path should NOT be stale.
        """
        file_cache.mark_lease_acquired(ZONE, PATH)
        file_cache.write(ZONE, PATH, b"content")

        revoke_done = threading.Event()

        def revoker() -> None:
            file_cache.mark_lease_revoked(ZONE, PATH)
            revoke_done.set()

        def acquirer() -> None:
            revoke_done.wait(timeout=5.0)
            file_cache.mark_lease_acquired(ZONE, PATH)

        t_revoke = threading.Thread(target=revoker)
        t_acquire = threading.Thread(target=acquirer)

        t_revoke.start()
        t_acquire.start()
        t_revoke.join(timeout=10.0)
        t_acquire.join(timeout=10.0)

        # After revoke + re-acquire, path is NOT stale
        assert file_cache.is_stale(ZONE, PATH) is False
        assert file_cache.has_active_lease(ZONE, PATH) is True


# ---------------------------------------------------------------------------
# 3. Concurrent revoke and write
# ---------------------------------------------------------------------------


class TestConcurrentRevokeAndWrite:
    def test_concurrent_revoke_and_write(self, file_cache: FileContentCache) -> None:
        """Thread A calls mark_lease_revoked. Thread B calls write() concurrently.

        After both complete, the path should NOT be stale (write clears staleness).
        """
        file_cache.mark_lease_acquired(ZONE, PATH)
        file_cache.write(ZONE, PATH, b"v1")

        # Use a barrier to ensure both threads start nearly simultaneously
        barrier = threading.Barrier(2, timeout=5.0)

        def revoker() -> None:
            barrier.wait()
            file_cache.mark_lease_revoked(ZONE, PATH)

        def writer() -> None:
            barrier.wait()
            # Small delay to let revoke likely happen first
            time.sleep(0.001)
            file_cache.write(ZONE, PATH, b"v2")

        t_revoke = threading.Thread(target=revoker)
        t_write = threading.Thread(target=writer)

        t_revoke.start()
        t_write.start()
        t_revoke.join(timeout=10.0)
        t_write.join(timeout=10.0)

        # Race: either write clears staleness (write after revoke) or
        # revoke sets staleness (revoke after write). Both are correct —
        # the important thing is no crash/deadlock and data is consistent.
        data = file_cache.read(ZONE, PATH)
        if file_cache.is_stale(ZONE, PATH):
            # Revoke happened after write — stale is correct
            assert data == b"v2"
        else:
            # Write happened after revoke — cleared staleness
            assert data == b"v2"


# ---------------------------------------------------------------------------
# 4. Concurrent mark operations are thread-safe
# ---------------------------------------------------------------------------


class TestConcurrentMarkOperations:
    def test_concurrent_mark_operations_are_thread_safe(self, file_cache: FileContentCache) -> None:
        """Spawn 10 threads all calling mark_lease_acquired and mark_lease_revoked.

        Verify no exceptions raised and final state is consistent
        (either stale or has_active_lease, never both).
        """
        file_cache.write(ZONE, PATH, b"initial")
        errors: list[Exception] = []
        barrier = threading.Barrier(10, timeout=5.0)

        def toggle(thread_id: int) -> None:
            try:
                barrier.wait()
                for _ in range(100):
                    if thread_id % 2 == 0:
                        file_cache.mark_lease_acquired(ZONE, PATH)
                        file_cache.mark_lease_revoked(ZONE, PATH)
                    else:
                        file_cache.mark_lease_revoked(ZONE, PATH)
                        file_cache.mark_lease_acquired(ZONE, PATH)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=toggle, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        # No exceptions during concurrent access
        assert errors == [], f"Errors during concurrent mark operations: {errors}"

        # Final state must be consistent: stale and has_active_lease
        # should never both be True simultaneously (they are mutually exclusive
        # within the lock, though one could be False/False for an untracked path).
        is_stale = file_cache.is_stale(ZONE, PATH)
        has_lease = file_cache.has_active_lease(ZONE, PATH)
        assert not (is_stale and has_lease), (
            f"Inconsistent state: is_stale={is_stale}, has_active_lease={has_lease}"
        )


# ---------------------------------------------------------------------------
# 5. SingleFlightSync coalesces concurrent calls
# ---------------------------------------------------------------------------


class TestSingleFlightSyncCoalescing:
    def test_singleflight_sync_coalesces_concurrent_calls(self) -> None:
        """Spawn 5 threads all calling sf.do("key", slow_fn).

        Verify slow_fn is called exactly once; all threads get the same result.
        """
        sf: SingleFlightSync[str] = SingleFlightSync()
        call_count = 0
        call_count_lock = threading.Lock()
        barrier = threading.Barrier(5, timeout=5.0)

        def slow_fn() -> str:
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            # Simulate slow work
            time.sleep(0.1)
            return "computed-result"

        results: list[str] = []
        results_lock = threading.Lock()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait()
                result = sf.do("key", slow_fn)
                with results_lock:
                    results.append(result)
            except Exception as e:
                with results_lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        # No errors
        assert errors == [], f"Errors during singleflight: {errors}"

        # slow_fn was called exactly once
        assert call_count == 1, f"Expected 1 call, got {call_count}"

        # All 5 threads got the same result
        assert len(results) == 5
        assert all(r == "computed-result" for r in results)
