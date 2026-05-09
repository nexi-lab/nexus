"""Cross-cache coherence tests for the FUSE logical cache split."""

import asyncio

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache
from nexus.fuse.cache import FUSECacheManager


@pytest.mark.asyncio
async def test_logical_file_cache_singleflight_keeps_one_origin_fill() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")
    calls = 0

    async def read_once() -> bytes | None:
        nonlocal calls
        lock = await cache.lock(key)
        async with lock:
            hit = await cache.get(key, expected_fingerprint="etag:1")
            if hit is None:
                calls += 1
                await asyncio.sleep(0.01)
                await cache.put(key, b"payload", fingerprint="etag:1")
        return await cache.get(key, expected_fingerprint="etag:1")

    assert await asyncio.gather(*(read_once() for _ in range(20))) == [b"payload"] * 20
    assert calls == 1


def test_content_cache_rejects_mismatched_fingerprint() -> None:
    cache = FUSECacheManager()

    cache.cache_content("/report.xlsx", b"raw-v1", fingerprint="etag:1")

    assert cache.get_content("/report.xlsx", expected_fingerprint="etag:2") is None
    assert cache.get_content("/report.xlsx", expected_fingerprint="etag:1") == b"raw-v1"


def test_invalidate_file_clears_raw_and_parsed_views_for_source_path() -> None:
    cache = FUSECacheManager(
        attr_cache_size=8,
        attr_cache_ttl=60,
        content_cache_size=8,
        parsed_cache_size=8,
    )
    cache.cache_content("/report.xlsx", b"raw", fingerprint="etag:1")
    cache.cache_parsed("/report.xlsx", "md", b"# parsed")

    cache.invalidate_file("/report.xlsx")

    assert cache.get_content("/report.xlsx", expected_fingerprint="etag:1") is None
    assert cache.get_parsed("/report.xlsx", "md") is None
