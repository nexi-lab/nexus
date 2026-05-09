"""Cross-cache coherence tests for the FUSE logical cache split."""

import asyncio
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache
from nexus.fuse.cache import FUSECacheManager
from nexus.fuse.lease_coordinator import FUSELeaseCoordinator
from nexus.fuse.ops._shared import FUSESharedContext, get_file_content
from nexus.storage.local_disk_cache import LocalDiskCache


class _Mode(Enum):
    BINARY = "binary"


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


def test_parent_only_invalidation_does_not_clear_grandparent_listing() -> None:
    cache = FUSECacheManager(
        attr_cache_size=8,
        attr_cache_ttl=60,
        content_cache_size=8,
        parsed_cache_size=8,
    )
    cache.cache_listing("/a", [".", "..", "b"])
    cache.cache_listing("/a/b", [".", "..", "c.txt"])

    cache.invalidate_parent_listing("/a/b/c.txt")

    assert cache.get_listing("/a") == [".", "..", "b"]
    assert cache.get_listing("/a/b") is None


@pytest.mark.asyncio
async def test_ttl_fallback_path_is_used_without_fingerprint() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("github_connector", "zone1", "/issues/1_test.yaml")

    await cache.put(key, b"cached", fingerprint=None, ttl_seconds=5)

    assert await cache.get(key, expected_fingerprint=None) == b"cached"


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


@pytest.mark.asyncio
async def test_fingerprintless_l2_ttl_fallback_expires_between_buckets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr("nexus.fuse.ops._shared.time.time", lambda: now[0])
    local_disk_cache = LocalDiskCache(cache_dir=tmp_path / "ldc", max_size_gb=0.01)
    versions = [b"v1"]
    nexus_fs = MagicMock()
    nexus_fs.zone_id = "zone1"
    nexus_fs.sys_stat.return_value = {"path": "/file.txt", "size": 2}
    nexus_fs.sys_read.side_effect = lambda path, context=None: versions[-1]
    cache = FUSELeaseCoordinator(
        FUSECacheManager(attr_cache_ttl=1),
        lease_manager=None,
    )
    ctx = FUSESharedContext(
        nexus_fs=nexus_fs,
        mode=_Mode.BINARY,
        context=None,
        namespace_manager=None,
        cache=cache,
        local_disk_cache=local_disk_cache,
        readahead=None,
        rust_client=None,
        use_rust=False,
        events=MagicMock(),
        cache_config={"attr_cache_ttl": 1},
    )
    try:
        first = await get_file_content(ctx, "/file.txt", None)
        versions[-1] = b"v2"
        now[0] = 2.1
        cache.invalidate_file("/file.txt")
        second = await get_file_content(ctx, "/file.txt", None)
    finally:
        cache.close()
        local_disk_cache.close()

    assert first == b"v1"
    assert second == b"v2"
    assert nexus_fs.sys_read.call_count == 2
