import asyncio

from nexus.cache.file_store import FileKey, MemoryFileCache


def _key(path: str) -> FileKey:
    return FileKey("test_backend", "default", path, "raw")


def _put(cache: MemoryFileCache, path: str, size: int) -> None:
    cache.put_sync(_key(path), b"x" * size, fingerprint=f"fp:{path}", ttl_seconds=60)


def test_byte_total_tracks_after_put():
    cache = MemoryFileCache(max_bytes=1024)
    _put(cache, "/a", 100)
    _put(cache, "/b", 200)
    assert cache.total_bytes == 300


def test_byte_total_decrements_on_invalidate():
    cache = MemoryFileCache(max_bytes=1024)
    _put(cache, "/a", 100)
    _put(cache, "/b", 200)
    cache.invalidate_sync(_key("/a"))
    assert cache.total_bytes == 200


def test_byte_total_handles_overwrite():
    cache = MemoryFileCache(max_bytes=1024)
    _put(cache, "/a", 100)
    _put(cache, "/a", 300)
    assert cache.total_bytes == 300


def test_evicts_lru_when_over_cap():
    cache = MemoryFileCache(max_bytes=300)
    _put(cache, "/a", 100)
    _put(cache, "/b", 100)
    _put(cache, "/c", 100)
    _put(cache, "/d", 100)  # forces eviction of /a
    assert cache.get_sync(_key("/a"), "fp:/a") is None
    assert cache.get_sync(_key("/d"), "fp:/d") == b"x" * 100
    assert cache.total_bytes == 300


def test_get_touches_lru():
    cache = MemoryFileCache(max_bytes=300)
    _put(cache, "/a", 100)
    _put(cache, "/b", 100)
    _put(cache, "/c", 100)
    # Touch /a to make it MRU
    assert cache.get_sync(_key("/a"), "fp:/a") == b"x" * 100
    _put(cache, "/d", 100)  # should evict /b, not /a
    assert cache.get_sync(_key("/a"), "fp:/a") == b"x" * 100
    assert cache.get_sync(_key("/b"), "fp:/b") is None


def test_oversize_entry_rejected(caplog):
    cache = MemoryFileCache(max_bytes=100)
    cache.put_sync(_key("/big"), b"x" * 500, fingerprint="fp:/big", ttl_seconds=60)
    assert cache.get_sync(_key("/big"), "fp:/big") is None
    assert cache.total_bytes == 0


def test_entry_equal_to_cap_allowed():
    cache = MemoryFileCache(max_bytes=100)
    _put(cache, "/a", 50)
    _put(cache, "/full", 100)  # evicts /a, fits exactly
    assert cache.get_sync(_key("/full"), "fp:/full") == b"x" * 100
    assert cache.total_bytes == 100


def test_existing_singleflight_still_works():
    """Sanity: existing fingerprint/TTL behavior preserved."""
    cache = MemoryFileCache(max_bytes=1024)
    asyncio.run(_singleflight_inner(cache))


async def _singleflight_inner(cache: MemoryFileCache) -> None:
    key = _key("/single")
    lock = await cache.lock(key)
    async with lock:
        await cache.put(key, b"payload", "fp:single", 60)
    assert await cache.get(key, "fp:single") == b"payload"
