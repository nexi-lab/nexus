"""Unit tests for VFS Counting Semaphore (Issue #908).

Tests both the Rust-accelerated and pure-Python implementations to verify
identical semantics.
"""

import threading
import time

import pytest

from nexus.core.semaphore import (
    PythonVFSSemaphore,
    VFSSemaphoreProtocol,
    create_vfs_semaphore,
)

# ---------------------------------------------------------------------------
# Fixtures — parametrize over both implementations
# ---------------------------------------------------------------------------

_IMPLEMENTATIONS: list[type] = [PythonVFSSemaphore]

try:
    from nexus.core.semaphore import RustVFSSemaphore

    _IMPLEMENTATIONS.append(RustVFSSemaphore)
except (ImportError, Exception):
    pass


@pytest.fixture(params=_IMPLEMENTATIONS, ids=lambda cls: cls.__name__)
def sem(request: pytest.FixtureRequest) -> VFSSemaphoreProtocol:
    return request.param()


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


class TestBasicAcquireRelease:
    def test_acquire_returns_uuid_string(self, sem: VFSSemaphoreProtocol) -> None:
        hid = sem.acquire("test", max_holders=1)
        assert hid is not None
        assert isinstance(hid, str)
        assert len(hid) == 36  # UUID4: 8-4-4-4-12
        assert hid.count("-") == 4
        sem.release("test", hid)

    def test_release_returns_true(self, sem: VFSSemaphoreProtocol) -> None:
        hid = sem.acquire("test", max_holders=1)
        assert hid is not None
        assert sem.release("test", hid)

    def test_double_release_returns_false(self, sem: VFSSemaphoreProtocol) -> None:
        hid = sem.acquire("test", max_holders=1)
        assert hid is not None
        assert sem.release("test", hid)
        assert not sem.release("test", hid)

    def test_release_unknown_name(self, sem: VFSSemaphoreProtocol) -> None:
        hid = sem.acquire("test", max_holders=1)
        assert hid is not None
        assert not sem.release("other", hid)
        sem.release("test", hid)

    def test_release_unknown_holder_id(self, sem: VFSSemaphoreProtocol) -> None:
        hid = sem.acquire("test", max_holders=1)
        assert hid is not None
        assert not sem.release("test", "nonexistent-id")
        sem.release("test", hid)


# ---------------------------------------------------------------------------
# Multiple holders
# ---------------------------------------------------------------------------


class TestMultipleHolders:
    def test_n_holders_up_to_max(self, sem: VFSSemaphoreProtocol) -> None:
        h1 = sem.acquire("test", max_holders=3)
        h2 = sem.acquire("test", max_holders=3)
        h3 = sem.acquire("test", max_holders=3)
        assert h1 is not None
        assert h2 is not None
        assert h3 is not None
        assert len({h1, h2, h3}) == 3  # all unique
        sem.release("test", h1)
        sem.release("test", h2)
        sem.release("test", h3)

    def test_n_plus_1_blocked(self, sem: VFSSemaphoreProtocol) -> None:
        holders = []
        for _ in range(3):
            h = sem.acquire("test", max_holders=3)
            assert h is not None
            holders.append(h)

        # 4th should fail (non-blocking)
        assert sem.acquire("test", max_holders=3, timeout_ms=0) is None

        for h in holders:
            sem.release("test", h)

    def test_release_one_then_acquire_succeeds(self, sem: VFSSemaphoreProtocol) -> None:
        h1 = sem.acquire("test", max_holders=2)
        h2 = sem.acquire("test", max_holders=2)
        assert h1 is not None
        assert h2 is not None

        # Full
        assert sem.acquire("test", max_holders=2) is None

        # Release one
        sem.release("test", h1)

        # Now should succeed
        h3 = sem.acquire("test", max_holders=2)
        assert h3 is not None

        sem.release("test", h2)
        sem.release("test", h3)


# ---------------------------------------------------------------------------
# SSOT enforcement
# ---------------------------------------------------------------------------


class TestSSOT:
    def test_mismatch_raises_value_error(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=3)
        assert h is not None
        with pytest.raises(ValueError, match="max_holders mismatch"):
            sem.acquire("test", max_holders=5)
        sem.release("test", h)

    def test_same_max_holders_ok(self, sem: VFSSemaphoreProtocol) -> None:
        h1 = sem.acquire("test", max_holders=3)
        h2 = sem.acquire("test", max_holders=3)
        assert h1 is not None
        assert h2 is not None
        sem.release("test", h1)
        sem.release("test", h2)

    def test_after_full_release_new_max_holders_ok(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=3)
        assert h is not None
        sem.release("test", h)

        # Entry cleaned up → new max_holders accepted
        h2 = sem.acquire("test", max_holders=5)
        assert h2 is not None
        sem.release("test", h2)

    def test_max_holders_less_than_1_raises(self, sem: VFSSemaphoreProtocol) -> None:
        with pytest.raises(ValueError):
            sem.acquire("test", max_holders=0)


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    def test_expired_holder_evicted_on_acquire(self, sem: VFSSemaphoreProtocol) -> None:
        # Acquire with very short TTL
        h = sem.acquire("test", max_holders=1, ttl_ms=5)
        assert h is not None

        # Wait for expiry
        time.sleep(0.02)

        # Should succeed (expired holder evicted lazily)
        h2 = sem.acquire("test", max_holders=1, ttl_ms=30_000)
        assert h2 is not None
        sem.release("test", h2)

    def test_non_expired_holder_blocks(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1, ttl_ms=30_000)
        assert h is not None
        # Should fail immediately (holder has 30s TTL)
        assert sem.acquire("test", max_holders=1, timeout_ms=0) is None
        sem.release("test", h)


# ---------------------------------------------------------------------------
# Extend
# ---------------------------------------------------------------------------


class TestExtend:
    def test_extend_keeps_holder_alive(self, sem: VFSSemaphoreProtocol) -> None:
        # Acquire with short TTL
        h = sem.acquire("test", max_holders=1, ttl_ms=10)
        assert h is not None

        # Extend to much longer
        assert sem.extend("test", h, ttl_ms=30_000)

        # Wait past original TTL
        time.sleep(0.02)

        # Should still be held (was extended)
        assert sem.acquire("test", max_holders=1, timeout_ms=0) is None
        sem.release("test", h)

    def test_extend_unknown_name(self, sem: VFSSemaphoreProtocol) -> None:
        assert not sem.extend("nonexistent", "fake-id")

    def test_extend_wrong_holder_id(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1)
        assert h is not None
        assert not sem.extend("test", "wrong-id")
        sem.release("test", h)


# ---------------------------------------------------------------------------
# Force release
# ---------------------------------------------------------------------------


class TestForceRelease:
    def test_force_release_clears_all(self, sem: VFSSemaphoreProtocol) -> None:
        h1 = sem.acquire("test", max_holders=3)
        h2 = sem.acquire("test", max_holders=3)
        assert h1 is not None
        assert h2 is not None
        assert sem.active_semaphores == 1

        assert sem.force_release("test")
        assert sem.active_semaphores == 0

        # Can acquire again
        h3 = sem.acquire("test", max_holders=3)
        assert h3 is not None
        sem.release("test", h3)

    def test_force_release_nonexistent(self, sem: VFSSemaphoreProtocol) -> None:
        assert not sem.force_release("nonexistent")


# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------


class TestInfo:
    def test_info_none_when_no_semaphore(self, sem: VFSSemaphoreProtocol) -> None:
        assert sem.info("nonexistent") is None

    def test_info_structure(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=3, ttl_ms=30_000)
        assert h is not None

        info = sem.info("test")
        assert info is not None
        assert info["name"] == "test"
        assert info["max_holders"] == 3
        assert info["active_count"] == 1
        assert isinstance(info["holders"], list)
        assert len(info["holders"]) == 1

        holder = info["holders"][0]
        assert holder["holder_id"] == h
        assert "acquired_at_ns" in holder
        assert "expires_at_ns" in holder

        sem.release("test", h)

    def test_info_none_after_release(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1)
        assert h is not None
        sem.release("test", h)
        assert sem.info("test") is None

    def test_info_evicts_expired(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1, ttl_ms=5)
        assert h is not None
        time.sleep(0.02)
        # info() should evict and return None
        assert sem.info("test") is None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_keys(self, sem: VFSSemaphoreProtocol) -> None:
        s = sem.stats()
        expected_keys = {
            "acquire_count",
            "release_count",
            "timeout_count",
            "active_semaphores",
            "active_holders",
        }
        assert expected_keys.issubset(set(s.keys()))

    def test_stats_after_operations(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1)
        assert h is not None
        sem.release("test", h)

        s = sem.stats()
        assert s["acquire_count"] >= 1
        assert s["release_count"] >= 1

    def test_stats_timeout_count(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1)
        assert h is not None
        # This should timeout
        sem.acquire("test", max_holders=1, timeout_ms=0)

        s = sem.stats()
        assert s["timeout_count"] >= 1
        sem.release("test", h)

    def test_stats_active_holders(self, sem: VFSSemaphoreProtocol) -> None:
        h1 = sem.acquire("a", max_holders=2)
        h2 = sem.acquire("a", max_holders=2)
        h3 = sem.acquire("b", max_holders=1)
        assert h1 is not None
        assert h2 is not None
        assert h3 is not None

        s = sem.stats()
        assert s["active_semaphores"] == 2
        assert s["active_holders"] == 3

        sem.release("a", h1)
        sem.release("a", h2)
        sem.release("b", h3)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_acquire_respects_max(self, sem: VFSSemaphoreProtocol) -> None:
        """20 threads try to acquire semaphore with max_holders=5."""
        results: list[str | None] = []
        lock = threading.Lock()

        def worker() -> None:
            hid = sem.acquire("shared", max_holders=5, timeout_ms=0)
            with lock:
                results.append(hid)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        successes = [r for r in results if r is not None]
        assert len(successes) == 5
        assert len(set(successes)) == 5  # all unique UUIDs

        for hid in successes:
            sem.release("shared", hid)

    def test_concurrent_mutex(self, sem: VFSSemaphoreProtocol) -> None:
        """20 threads try to acquire max_holders=1 — exactly one succeeds."""
        results: list[str | None] = []
        lock = threading.Lock()

        def worker() -> None:
            hid = sem.acquire("mutex", max_holders=1, timeout_ms=0)
            with lock:
                results.append(hid)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        successes = [r for r in results if r is not None]
        assert len(successes) == 1

        sem.release("mutex", successes[0])


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_nonblocking_returns_none(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1)
        assert h is not None
        result = sem.acquire("test", max_holders=1, timeout_ms=0)
        assert result is None
        sem.release("test", h)

    def test_blocking_timeout_returns_none(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1)
        assert h is not None
        start = time.monotonic()
        result = sem.acquire("test", max_holders=1, timeout_ms=50)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert result is None
        assert elapsed_ms >= 40  # allow slack
        sem.release("test", h)

    def test_blocking_succeeds_when_released(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("test", max_holders=1)
        assert h is not None
        result_holder: list[str | None] = []

        def release_later() -> None:
            time.sleep(0.02)
            sem.release("test", h)

        t = threading.Thread(target=release_later)
        t.start()

        hid = sem.acquire("test", max_holders=1, timeout_ms=500)
        result_holder.append(hid)
        t.join()

        assert result_holder[0] is not None
        sem.release("test", result_holder[0])


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_max_holders_one_acts_as_mutex(self, sem: VFSSemaphoreProtocol) -> None:
        h1 = sem.acquire("mutex", max_holders=1)
        assert h1 is not None
        assert sem.acquire("mutex", max_holders=1, timeout_ms=0) is None
        sem.release("mutex", h1)

        h2 = sem.acquire("mutex", max_holders=1)
        assert h2 is not None
        sem.release("mutex", h2)

    def test_multiple_independent_semaphores(self, sem: VFSSemaphoreProtocol) -> None:
        ha = sem.acquire("a", max_holders=1)
        hb = sem.acquire("b", max_holders=1)
        assert ha is not None
        assert hb is not None
        assert sem.active_semaphores == 2
        sem.release("a", ha)
        sem.release("b", hb)

    def test_empty_cleanup(self, sem: VFSSemaphoreProtocol) -> None:
        h = sem.acquire("temp", max_holders=1)
        assert h is not None
        assert sem.active_semaphores == 1
        sem.release("temp", h)
        assert sem.active_semaphores == 0


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestFactory:
    def test_create_returns_protocol(self) -> None:
        s = create_vfs_semaphore()
        assert isinstance(s, VFSSemaphoreProtocol)

    def test_factory_functional(self) -> None:
        s = create_vfs_semaphore()
        h = s.acquire("test", max_holders=1)
        assert h is not None
        assert s.release("test", h)
