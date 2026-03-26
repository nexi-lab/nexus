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
    return LocalLockManager(sem, zone_id="z1")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_release(mgr):
    """acquire returns lock_id, release returns True."""
    lock_id = await mgr.acquire("/file.txt")
    assert lock_id is not None
    assert isinstance(lock_id, str)
    assert len(lock_id) == 36  # UUID format

    released = await mgr.release(lock_id, "/file.txt")
    assert released is True


@pytest.mark.asyncio
async def test_acquire_timeout_zero(mgr):
    """acquire with timeout=0 returns None on contention."""
    # Hold a lock so the second acquire contends
    first = await mgr.acquire("/busy")
    assert first is not None

    result = await mgr.acquire("/busy", timeout=0)
    assert result is None

    # Cleanup
    await mgr.release(first, "/busy")


@pytest.mark.asyncio
async def test_acquire_retry_succeeds(mgr, sem):
    """acquire retries until success within timeout."""
    import asyncio

    # Hold a lock, then release it after a short delay
    first = await mgr.acquire("/contested")
    assert first is not None

    async def release_after_delay():
        await asyncio.sleep(0.05)
        await mgr.release(first, "/contested")

    task = asyncio.create_task(release_after_delay())
    lock_id = await mgr.acquire("/contested", timeout=2.0)
    assert lock_id is not None
    await task

    # Cleanup
    await mgr.release(lock_id, "/contested")


@pytest.mark.asyncio
async def test_acquire_retry_timeout(mgr):
    """acquire returns None when timeout expires during retries."""
    # Hold a lock and never release it
    first = await mgr.acquire("/held")
    assert first is not None

    t0 = time.monotonic()
    result = await mgr.acquire("/held", timeout=0.15)
    elapsed = time.monotonic() - t0

    assert result is None
    assert elapsed >= 0.1  # at least a couple retries

    # Cleanup
    await mgr.release(first, "/held")


@pytest.mark.asyncio
async def test_extend(mgr):
    """extend returns ExtendResult with success=True."""
    lock_id = await mgr.acquire("/file.txt")
    assert lock_id is not None

    result = await mgr.extend(lock_id, "/file.txt", ttl=60.0)
    assert isinstance(result, ExtendResult)
    assert result.success is True
    assert result.lock_info is not None
    assert result.lock_info.path == "/file.txt"

    # Cleanup
    await mgr.release(lock_id, "/file.txt")


@pytest.mark.asyncio
async def test_extend_failure(mgr):
    """extend returns success=False when lock_id is unknown."""
    result = await mgr.extend("bad-id", "/file.txt")
    assert result.success is False
    assert result.lock_info is None


@pytest.mark.asyncio
async def test_release_unknown(mgr):
    """release unknown lock_id returns False."""
    released = await mgr.release("nonexistent", "/file.txt")
    assert released is False


@pytest.mark.asyncio
async def test_semaphore_mode(mgr):
    """acquire with max_holders>1 allows multiple holders."""
    holder1 = await mgr.acquire("/slots", max_holders=5)
    holder2 = await mgr.acquire("/slots", max_holders=5)
    assert holder1 is not None
    assert holder2 is not None
    assert holder1 != holder2

    # Cleanup
    await mgr.release(holder1, "/slots")
    await mgr.release(holder2, "/slots")


@pytest.mark.asyncio
async def test_health_check(mgr):
    """health_check always returns True for local semaphore."""
    assert await mgr.health_check() is True


@pytest.mark.asyncio
async def test_force_release(mgr):
    """force_release removes all holders."""
    lock_id = await mgr.acquire("/locked")
    assert lock_id is not None

    result = await mgr.force_release("/locked")
    assert result is True

    # Should no longer be locked
    assert await mgr.is_locked("/locked") is False


@pytest.mark.asyncio
async def test_force_release_no_lock(mgr):
    """force_release returns False when no lock exists."""
    result = await mgr.force_release("/not-locked")
    assert result is False


@pytest.mark.asyncio
async def test_get_lock_info_none(mgr):
    """get_lock_info returns None when not locked."""
    info = await mgr.get_lock_info("/file.txt")
    assert info is None


@pytest.mark.asyncio
async def test_get_lock_info_with_data(mgr):
    """get_lock_info returns LockInfo when locked."""
    lock_id = await mgr.acquire("/file.txt")
    assert lock_id is not None

    info = await mgr.get_lock_info("/file.txt")
    assert info is not None
    assert info.path == "/file.txt"
    assert info.mode == "mutex"
    assert len(info.holders) == 1
    assert info.holders[0].lock_id == lock_id

    # Cleanup
    await mgr.release(lock_id, "/file.txt")


@pytest.mark.asyncio
async def test_get_lock_info_semaphore(mgr):
    """get_lock_info reports mode='semaphore' when multiple holders."""
    h1 = await mgr.acquire("/slots", max_holders=3)
    h2 = await mgr.acquire("/slots", max_holders=3)
    assert h1 is not None
    assert h2 is not None

    info = await mgr.get_lock_info("/slots")
    assert info is not None
    assert info.mode == "semaphore"

    # Cleanup
    await mgr.release(h1, "/slots")
    await mgr.release(h2, "/slots")


@pytest.mark.asyncio
async def test_list_locks_empty(mgr):
    """list_locks returns empty list when no locks."""
    locks = await mgr.list_locks()
    assert locks == []


@pytest.mark.asyncio
async def test_list_locks_with_pattern(mgr):
    """list_locks filters by pattern."""
    h1 = await mgr.acquire("/a/file.txt")
    h2 = await mgr.acquire("/b/other.txt")
    assert h1 is not None
    assert h2 is not None

    locks = await mgr.list_locks(pattern="/a/")
    assert len(locks) == 1
    assert locks[0].path == "/a/file.txt"

    # Cleanup
    await mgr.release(h1, "/a/file.txt")
    await mgr.release(h2, "/b/other.txt")


@pytest.mark.asyncio
async def test_is_locked(mgr):
    """is_locked returns True when locked, False otherwise."""
    assert await mgr.is_locked("/file.txt") is False

    lock_id = await mgr.acquire("/file.txt")
    assert lock_id is not None
    assert await mgr.is_locked("/file.txt") is True

    # Cleanup
    await mgr.release(lock_id, "/file.txt")


@pytest.mark.asyncio
async def test_max_holders_validation(mgr):
    """max_holders < 1 raises ValueError."""
    with pytest.raises(ValueError, match="max_holders must be >= 1"):
        await mgr.acquire("/file.txt", max_holders=0)


# ---------------------------------------------------------------------------
# Shared / Exclusive mode tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_locks_coexist(mgr):
    """Two shared locks on same path succeed."""
    h1 = await mgr.acquire("/shared.txt", mode="shared")
    h2 = await mgr.acquire("/shared.txt", mode="shared")
    assert h1 is not None
    assert h2 is not None
    assert h1 != h2

    # Both should be tracked
    assert await mgr.is_locked("/shared.txt") is True

    # Cleanup
    await mgr.release(h1, "/shared.txt")
    await mgr.release(h2, "/shared.txt")


@pytest.mark.asyncio
async def test_exclusive_blocks_shared(mgr):
    """An exclusive lock blocks new shared locks (attempt with timeout=0 should fail)."""
    excl = await mgr.acquire("/exclusive.txt", mode="exclusive")
    assert excl is not None

    # Shared lock should fail with timeout=0 because exclusive holds the gate
    shared = await mgr.acquire("/exclusive.txt", mode="shared", timeout=0)
    assert shared is None

    # Cleanup
    await mgr.release(excl, "/exclusive.txt")
