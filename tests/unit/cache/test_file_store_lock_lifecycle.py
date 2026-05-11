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
