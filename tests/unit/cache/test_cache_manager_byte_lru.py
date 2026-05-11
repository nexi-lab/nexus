import logging

import pytest

from nexus.fuse.cache import FUSECacheManager


def test_cache_manager_accepts_byte_knobs():
    mgr = FUSECacheManager(
        content_cache_bytes=64 * 1024 * 1024,
        parsed_cache_bytes=8 * 1024 * 1024,
        max_drain_bytes=1024 * 1024,
    )
    assert mgr._file_cache.max_bytes == 72 * 1024 * 1024
    assert mgr.max_drain_bytes == 1024 * 1024


def test_max_drain_bytes_default_safe():
    mgr = FUSECacheManager()
    # Defaults: 512MB content + 64MB parsed = 576MB total. drain default 16MB.
    assert mgr.max_drain_bytes == 16 * 1024 * 1024


def test_cache_content_skips_oversize(caplog):
    mgr = FUSECacheManager(
        content_cache_bytes=4 * 1024,
        parsed_cache_bytes=0,
        max_drain_bytes=1024,
        enable_metrics=True,
    )
    big = b"x" * 4096
    with caplog.at_level(logging.WARNING):
        mgr.cache_content("/big.bin", big, fingerprint="fp", ttl_seconds=60)
    assert mgr.get_content("/big.bin", expected_fingerprint="fp") is None
    assert mgr._metrics["content_skipped_oversize"] == 1


def test_cache_content_accepts_under_cap():
    mgr = FUSECacheManager(
        content_cache_bytes=4 * 1024,
        parsed_cache_bytes=0,
        max_drain_bytes=2048,
    )
    mgr.cache_content("/small.bin", b"x" * 1000, fingerprint="fp", ttl_seconds=60)
    assert mgr.get_content("/small.bin", expected_fingerprint="fp") == b"x" * 1000


def test_max_drain_bytes_exceeds_total_raises():
    with pytest.raises(ValueError, match="max_drain_bytes"):
        FUSECacheManager(
            content_cache_bytes=1024,
            parsed_cache_bytes=0,
            max_drain_bytes=2048,
        )


def test_ttl_overrides_threaded_through():
    mgr = FUSECacheManager(
        index_ttl_overrides={"path_s3": 30},
    )
    assert mgr.index_ttl_for_backend("path_s3") == 30
    assert mgr.index_ttl_for_backend("path_gcs") == 600


def test_oversize_content_invalidates_prior_entry():
    """A rejected oversized replacement must not leave stale bytes cached."""
    mgr = FUSECacheManager(
        content_cache_bytes=4 * 1024,
        parsed_cache_bytes=0,
        max_drain_bytes=1024,
    )
    mgr.cache_content("/x", b"old" * 50, fingerprint=None, ttl_seconds=60)
    assert mgr.get_content("/x") == b"old" * 50
    mgr.cache_content("/x", b"new" * 2000, fingerprint=None, ttl_seconds=60)
    assert mgr.get_content("/x") is None


def test_ttl_override_applies_to_cache_attr():
    """index_ttl_overrides must affect actual metadata cache TTL."""
    fake_now = [1000.0]
    mgr = FUSECacheManager(index_ttl_overrides={"path_s3": 5})
    mgr._index_cache._now_fn = lambda: fake_now[0]

    mgr.cache_attr("/s3/file", {"st_size": 1}, backend_id="path_s3")
    fake_now[0] = 1004.0
    assert mgr.get_attr("/s3/file") == {"st_size": 1}
    fake_now[0] = 1006.0
    assert mgr.get_attr("/s3/file") is None


def test_ttl_override_applies_to_cache_listing():
    fake_now = [1000.0]
    mgr = FUSECacheManager(index_ttl_overrides={"path_s3": 5})
    mgr._index_cache._now_fn = lambda: fake_now[0]

    mgr.cache_listing("/s3/dir", ["a"], backend_id="path_s3")
    fake_now[0] = 1004.0
    assert mgr.get_listing("/s3/dir") == ["a"]
    fake_now[0] = 1006.0
    assert mgr.get_listing("/s3/dir") is None


def test_index_cache_entry_count_bound():
    """Safety net: many distinct paths under TTL don't grow without bound."""
    from nexus.cache.index_store import IndexKey, MemoryIndexCache

    cache = MemoryIndexCache(max_entries=3)
    for i in range(10):
        cache.put(IndexKey("b", "d", f"/p{i}", "stat"), {"i": i}, ttl_seconds=600)
    assert len(cache._entries) == 3
    assert cache.get(IndexKey("b", "d", "/p0", "stat")) is None
    assert cache.get(IndexKey("b", "d", "/p9", "stat")) == {"i": 9}


def test_ttl_policy_fallback_for_resolved_backend_without_override():
    """A resolved backend_id with no explicit override uses the policy default,
    not the caller's generic default."""
    fake_now = [1000.0]
    # path_local policy default is 0 (never cache). Override absent.
    mgr = FUSECacheManager(attr_cache_ttl=60)  # generic default 60s
    mgr._index_cache._now_fn = lambda: fake_now[0]

    mgr.cache_attr("/local/file", {"st_size": 1}, backend_id="path_local")
    # TTL=0 means the entry is immediately expired by the policy fallback.
    # _now_fn unchanged → policy.index_ttl_for_backend('path_local') returns 0
    # which results in expires_at == now, get() returns None on first read.
    assert mgr.get_attr("/local/file") is None


def test_ttl_policy_default_for_path_s3_without_override():
    """path_s3 with no override should get the policy default (600s)."""
    fake_now = [1000.0]
    mgr = FUSECacheManager(attr_cache_ttl=60)  # generic 60s
    mgr._index_cache._now_fn = lambda: fake_now[0]

    mgr.cache_attr("/s3/file", {"st_size": 1}, backend_id="path_s3")
    fake_now[0] = 1300.0  # 300s elapsed, well under 600s policy default
    assert mgr.get_attr("/s3/file") == {"st_size": 1}
    fake_now[0] = 1700.0  # past 600s
    assert mgr.get_attr("/s3/file") is None


def test_inflight_future_coalesces_oversize_readers():
    """100 concurrent readers of oversize content share a single result."""
    import asyncio

    mgr = FUSECacheManager(content_cache_bytes=4096, parsed_cache_bytes=0, max_drain_bytes=1024)

    async def scenario() -> tuple[int, list[bytes]]:
        fetches = 0
        big_content = b"x" * 8192

        async def reader() -> bytes:
            nonlocal fetches
            fut, is_owner = mgr.inflight_future("/big.bin", "fp:v1")
            if not is_owner:
                return await asyncio.wrap_future(fut)
            try:
                fetches += 1
                await asyncio.sleep(0.005)
                fut.set_result(big_content)
                return big_content
            finally:
                mgr.inflight_clear("/big.bin", "fp:v1")

        results = await asyncio.gather(*[reader() for _ in range(50)])
        return fetches, results

    fetches, results = asyncio.run(scenario())
    assert fetches == 1, f"expected 1 fetch (singleflight), got {fetches}"
    assert all(r == b"x" * 8192 for r in results)


def test_inflight_future_distinct_for_changed_fingerprint():
    """A fingerprint change in flight must NOT serve stale bytes to the new reader."""

    mgr = FUSECacheManager()
    f1, owner1 = mgr.inflight_future("/p", "fp:v1")
    f2, owner2 = mgr.inflight_future("/p", "fp:v2")
    assert owner1 is True and owner2 is True
    assert f1 is not f2
    f1.set_result(b"v1-bytes")
    f2.set_result(b"v2-bytes")
    assert f1.result() == b"v1-bytes"
    assert f2.result() == b"v2-bytes"
    mgr.inflight_clear("/p", "fp:v1")
    mgr.inflight_clear("/p", "fp:v2")


def test_inflight_future_cross_event_loop():
    """Two FUSE syscalls (each driving their own asyncio.run) must share the future."""
    import asyncio
    import threading

    mgr = FUSECacheManager()
    results: list[bytes | Exception] = []
    barrier = threading.Barrier(2)

    def owner_thread() -> None:
        async def owner() -> None:
            fut, is_owner = mgr.inflight_future("/x", "fp")
            assert is_owner
            barrier.wait()  # let waiter register
            await asyncio.sleep(0.02)
            fut.set_result(b"shared")

        asyncio.run(owner())

    def waiter_thread() -> None:
        async def waiter() -> None:
            barrier.wait()
            # Second call sees the owner's future
            fut, is_owner = mgr.inflight_future("/x", "fp")
            assert not is_owner
            data = await asyncio.wrap_future(fut)
            results.append(data)

        try:
            asyncio.run(waiter())
        except Exception as e:
            results.append(e)

    t1 = threading.Thread(target=owner_thread)
    t2 = threading.Thread(target=waiter_thread)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    mgr.inflight_clear("/x", "fp")

    assert results == [b"shared"], f"unexpected results: {results}"


def test_backend_id_for_path_resolver_fallback_chain():
    """backend_id_for_path tries backend_name → name → _backend_name."""
    from nexus.fuse.ops._shared import backend_id_for_path

    class _BackendNameAttr:
        backend_name = "path_s3"

    class _NameAttr:
        name = "path_gcs"

    class _PrivateAttr:
        _backend_name = "github_connector"

    class _FakeFS:
        def __init__(self, mounts):
            self._mounted_backend_instances = mounts

    class _FakeCtx:
        def __init__(self, mounts):
            self.nexus_fs = _FakeFS(mounts)

    class _CLIGitHubAttr:
        name = "cli:gh"

    mounts = {
        "/s3": _BackendNameAttr(),
        "/gcs": _NameAttr(),
        "/gh": _PrivateAttr(),
        "/github-mount": _CLIGitHubAttr(),
    }
    ctx = _FakeCtx(mounts)
    assert backend_id_for_path(ctx, "/s3/file.txt") == "path_s3"
    assert backend_id_for_path(ctx, "/gcs/dir/x") == "path_gcs"
    assert backend_id_for_path(ctx, "/gh/repo/x") == "github_connector"
    # cli:gh must normalize to github_connector so policy TTL applies.
    assert backend_id_for_path(ctx, "/github-mount/file") == "github_connector"
    assert backend_id_for_path(ctx, "/unmounted/x") is None
