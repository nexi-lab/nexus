import asyncio

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache


@pytest.mark.asyncio
async def test_memory_file_cache_rejects_mismatched_fingerprint() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")

    await cache.put(key, b"old-bytes", fingerprint="etag:old")
    assert await cache.get(key, expected_fingerprint="etag:new") is None


@pytest.mark.asyncio
async def test_memory_file_cache_uses_ttl_fallback_without_fingerprint() -> None:
    now = [100.0]
    cache = MemoryFileCache(now_fn=lambda: now[0])
    key = FileKey("github_connector", "zone1", "/issues/1_test.yaml")

    await cache.put(key, b"cached", fingerprint=None, ttl_seconds=5)
    assert await cache.get(key, expected_fingerprint=None) == b"cached"

    now[0] += 6
    assert await cache.get(key, expected_fingerprint=None) is None


@pytest.mark.asyncio
async def test_memory_file_cache_rejects_unvalidated_hit_without_ttl() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")

    await cache.put(key, b"cached", fingerprint="etag:1", ttl_seconds=None)

    assert await cache.get(key, expected_fingerprint=None) is None
    assert await cache.get(key, expected_fingerprint="etag:1") == b"cached"


@pytest.mark.asyncio
async def test_memory_file_cache_singleflight_allows_one_fill() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")
    fill_calls = 0

    async def worker() -> bytes | None:
        nonlocal fill_calls
        lock = await cache.lock(key)
        async with lock:
            hit = await cache.get(key, expected_fingerprint="etag:1")
            if hit is None:
                fill_calls += 1
                await asyncio.sleep(0.01)
                await cache.put(key, b"payload", fingerprint="etag:1")
        return await cache.get(key, expected_fingerprint="etag:1")

    results = await asyncio.gather(*(worker() for _ in range(100)))
    assert results == [b"payload"] * 100
    assert fill_calls == 1


@pytest.mark.asyncio
async def test_memory_file_cache_recent_reread_workload_hits_above_ninety_percent() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    hot_keys = [FileKey("path_s3", "zone1", f"/bucket/hot-{index}.txt") for index in range(10)]
    hits = 0
    misses = 0

    for index in range(250):
        key = hot_keys[index % len(hot_keys)]
        fingerprint = f"etag:{key.path}"
        cached = await cache.get(key, expected_fingerprint=fingerprint)
        if cached is None:
            misses += 1
            await cache.put(key, b"payload", fingerprint=fingerprint)
        else:
            hits += 1

    assert hits / (hits + misses) >= 0.90


@pytest.mark.asyncio
async def test_memory_file_cache_keeps_active_singleflight_lock_on_invalidate() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")
    active_lock = await cache.lock(key)

    async with active_lock:
        await cache.invalidate(key)
        next_lock = await cache.lock(key)

    assert next_lock is active_lock


@pytest.mark.asyncio
async def test_memory_file_cache_keeps_waiting_singleflight_lock_on_invalidate() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")
    active_lock = await cache.lock(key)
    await active_lock.acquire()

    async def waiter() -> None:
        async with active_lock:
            pass

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)

    active_lock.release()
    await cache.invalidate(key)
    next_lock = await cache.lock(key)

    await waiter_task

    assert next_lock is active_lock
