import asyncio

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache


def _key(path: str) -> FileKey:
    return FileKey("test_backend", "default", path, "raw")


@pytest.mark.asyncio
async def test_lock_dict_bounded_by_max_lock_entries():
    cache = MemoryFileCache(max_bytes=1024, max_lock_entries=4)
    for i in range(10):
        await cache.lock(_key(f"/k{i}"))
    # All locks are unheld → bound enforced exactly
    assert len(cache._locks) == 4


@pytest.mark.asyncio
async def test_held_lock_not_evicted():
    cache = MemoryFileCache(max_bytes=1024, max_lock_entries=2)
    held_key = _key("/held")
    held = await cache.lock(held_key)
    await held.acquire()
    try:
        for i in range(10):
            await cache.lock(_key(f"/k{i}"))
        assert held_key in cache._locks
    finally:
        held.release()


@pytest.mark.asyncio
async def test_singleflight_100_concurrent_cold_gets():
    cache = MemoryFileCache(max_bytes=10 * 1024 * 1024)
    key = _key("/hot")
    fetch_count = 0
    fetch_lock = asyncio.Lock()

    async def get_or_fill() -> bytes:
        nonlocal fetch_count
        lock = await cache.lock(key)
        async with lock:
            hit = await cache.get(key, "fp:hot")
            if hit is not None:
                return hit
            async with fetch_lock:
                fetch_count += 1
            await asyncio.sleep(0.01)  # simulate backend latency
            await cache.put(key, b"payload" * 1000, "fp:hot", 60)
            return b"payload" * 1000

    results = await asyncio.gather(*[get_or_fill() for _ in range(100)])
    assert all(r == b"payload" * 1000 for r in results)
    assert fetch_count == 1


@pytest.mark.asyncio
async def test_same_key_returns_same_lock_object():
    cache = MemoryFileCache(max_bytes=1024)
    key = _key("/x")
    l1 = await cache.lock(key)
    l2 = await cache.lock(key)
    assert l1 is l2


@pytest.mark.asyncio
async def test_lock_with_queued_waiter_survives_eviction_pressure():
    """A lock with a queued waiter must NOT be evicted under cap pressure.

    Regression: an earlier impl only checked `asyncio.Lock.locked()`, which
    is briefly false between release and the next acquire — exactly the window
    in which a queued waiter exists. Evicting the lock there reset singleflight.
    """
    cache = MemoryFileCache(max_bytes=1024, max_lock_entries=1)
    held_key = _key("/contended")
    other_key = _key("/other")
    fetches = 0
    initial_lock = await cache.lock(held_key)
    initial_acquired = asyncio.Event()
    proceed = asyncio.Event()

    async def initial_holder():
        async with initial_lock:
            initial_acquired.set()
            await proceed.wait()

    async def waiter():
        nonlocal fetches
        lock = await cache.lock(held_key)
        async with lock:
            fetches += 1

    holder_task = asyncio.create_task(initial_holder())
    await initial_acquired.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # let waiter enter cache.lock + queue on _lock

    # Eviction pressure: looking up another key triggers _evict_unused_locks
    await cache.lock(other_key)
    assert held_key in cache._locks, (
        "lock with queued waiter must not be evicted under cap pressure"
    )

    proceed.set()
    await holder_task
    await waiter_task

    # Waiter ran exactly once, on the same wrapper as initial holder.
    assert fetches == 1
    assert initial_lock is cache._locks.get(held_key) or held_key not in cache._locks
