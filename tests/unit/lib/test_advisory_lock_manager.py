"""Unit tests for LocalLockManager (standalone advisory lock manager).

Tests use a real PythonVFSSemaphore — no mocks needed.
"""

import time

import pytest

from nexus.lib.distributed_lock import ExtendResult, LocalLockManager
from nexus.lib.semaphore import PythonVFSSemaphore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sem():
    return PythonVFSSemaphore()


@pytest.fixture
def mgr(sem):
    return LocalLockManager(sem)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_acquire_release(mgr):
    """acquire returns lock_id, release returns True."""
    lock_id = mgr.acquire("/file.txt")
    assert lock_id is not None
    assert isinstance(lock_id, str)
    assert len(lock_id) == 36  # UUID format

    released = mgr.release(lock_id, "/file.txt")
    assert released is True


def test_acquire_timeout_zero(mgr):
    """acquire with timeout=0 returns None on contention."""
    # Hold a lock so the second acquire contends
    first = mgr.acquire("/busy")
    assert first is not None

    result = mgr.acquire("/busy", timeout=0)
    assert result is None

    # Cleanup
    mgr.release(first, "/busy")


def test_acquire_retry_succeeds(mgr, sem):
    """acquire retries until success within timeout."""
    import threading

    # Hold a lock, then release it after a short delay
    first = mgr.acquire("/contested")
    assert first is not None

    def release_after_delay():
        time.sleep(0.05)
        mgr.release(first, "/contested")

    t = threading.Thread(target=release_after_delay)
    t.start()
    lock_id = mgr.acquire("/contested", timeout=2.0)
    assert lock_id is not None
    t.join()

    # Cleanup
    mgr.release(lock_id, "/contested")


def test_acquire_retry_timeout(mgr):
    """acquire returns None when timeout expires during retries."""
    # Hold a lock and never release it
    first = mgr.acquire("/held")
    assert first is not None

    t0 = time.monotonic()
    result = mgr.acquire("/held", timeout=0.15)
    elapsed = time.monotonic() - t0

    assert result is None
    assert elapsed >= 0.1  # at least a couple retries

    # Cleanup
    mgr.release(first, "/held")


def test_extend(mgr):
    """extend returns ExtendResult with success=True."""
    lock_id = mgr.acquire("/file.txt")
    assert lock_id is not None

    result = mgr.extend(lock_id, "/file.txt", ttl=60.0)
    assert isinstance(result, ExtendResult)
    assert result.success is True
    assert result.lock_info is not None
    assert result.lock_info.path == "/file.txt"

    # Cleanup
    mgr.release(lock_id, "/file.txt")


def test_extend_failure(mgr):
    """extend returns success=False when lock_id is unknown."""
    result = mgr.extend("bad-id", "/file.txt")
    assert result.success is False
    assert result.lock_info is None


def test_release_unknown(mgr):
    """release unknown lock_id returns False."""
    released = mgr.release("nonexistent", "/file.txt")
    assert released is False


def test_semaphore_mode(mgr):
    """acquire with max_holders>1 allows multiple holders."""
    holder1 = mgr.acquire("/slots", max_holders=5)
    holder2 = mgr.acquire("/slots", max_holders=5)
    assert holder1 is not None
    assert holder2 is not None
    assert holder1 != holder2

    # Cleanup
    mgr.release(holder1, "/slots")
    mgr.release(holder2, "/slots")


def test_health_check(mgr):
    """health_check always returns True for local semaphore."""
    assert mgr.health_check() is True


def test_force_release(mgr):
    """force_release removes all holders."""
    lock_id = mgr.acquire("/locked")
    assert lock_id is not None

    result = mgr.force_release("/locked")
    assert result is True

    # Should no longer be locked
    assert mgr.is_locked("/locked") is False


def test_force_release_no_lock(mgr):
    """force_release returns False when no lock exists."""
    result = mgr.force_release("/not-locked")
    assert result is False


def test_get_lock_info_none(mgr):
    """get_lock_info returns None when not locked."""
    info = mgr.get_lock_info("/file.txt")
    assert info is None


def test_get_lock_info_with_data(mgr):
    """get_lock_info returns LockInfo when locked."""
    lock_id = mgr.acquire("/file.txt")
    assert lock_id is not None

    info = mgr.get_lock_info("/file.txt")
    assert info is not None
    assert info.path == "/file.txt"
    assert info.mode == "mutex"
    assert len(info.holders) == 1
    assert info.holders[0].lock_id == lock_id

    # Cleanup
    mgr.release(lock_id, "/file.txt")


def test_get_lock_info_semaphore(mgr):
    """get_lock_info reports mode='semaphore' when multiple holders."""
    h1 = mgr.acquire("/slots", max_holders=3)
    h2 = mgr.acquire("/slots", max_holders=3)
    assert h1 is not None
    assert h2 is not None

    info = mgr.get_lock_info("/slots")
    assert info is not None
    assert info.mode == "semaphore"

    # Cleanup
    mgr.release(h1, "/slots")
    mgr.release(h2, "/slots")


def test_list_locks_empty(mgr):
    """list_locks returns empty list when no locks."""
    locks = mgr.list_locks()
    assert locks == []


def test_list_locks_with_pattern(mgr):
    """list_locks filters by pattern."""
    h1 = mgr.acquire("/a/file.txt")
    h2 = mgr.acquire("/b/other.txt")
    assert h1 is not None
    assert h2 is not None

    locks = mgr.list_locks(pattern="/a/")
    assert len(locks) == 1
    assert locks[0].path == "/a/file.txt"

    # Cleanup
    mgr.release(h1, "/a/file.txt")
    mgr.release(h2, "/b/other.txt")


def test_is_locked(mgr):
    """is_locked returns True when locked, False otherwise."""
    assert mgr.is_locked("/file.txt") is False

    lock_id = mgr.acquire("/file.txt")
    assert lock_id is not None
    assert mgr.is_locked("/file.txt") is True

    # Cleanup
    mgr.release(lock_id, "/file.txt")


def test_max_holders_validation(mgr):
    """max_holders < 1 raises ValueError."""
    with pytest.raises(ValueError, match="max_holders must be >= 1"):
        mgr.acquire("/file.txt", max_holders=0)


# ---------------------------------------------------------------------------
# Shared / Exclusive mode tests
# ---------------------------------------------------------------------------


def test_shared_locks_coexist(mgr):
    """Two shared locks on same path succeed."""
    h1 = mgr.acquire("/shared.txt", mode="shared")
    h2 = mgr.acquire("/shared.txt", mode="shared")
    assert h1 is not None
    assert h2 is not None
    assert h1 != h2

    # Both should be tracked
    assert mgr.is_locked("/shared.txt") is True

    # Cleanup
    mgr.release(h1, "/shared.txt")
    mgr.release(h2, "/shared.txt")


def test_exclusive_blocks_shared(mgr):
    """An exclusive lock blocks new shared locks (attempt with timeout=0 should fail)."""
    excl = mgr.acquire("/exclusive.txt", mode="exclusive")
    assert excl is not None

    # Shared lock should fail with timeout=0 because exclusive holds the gate
    shared = mgr.acquire("/exclusive.txt", mode="shared", timeout=0)
    assert shared is None

    # Cleanup
    mgr.release(excl, "/exclusive.txt")
