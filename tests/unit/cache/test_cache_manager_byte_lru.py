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

    mounts = {
        "/s3": _BackendNameAttr(),
        "/gcs": _NameAttr(),
        "/gh": _PrivateAttr(),
    }
    ctx = _FakeCtx(mounts)
    assert backend_id_for_path(ctx, "/s3/file.txt") == "path_s3"
    assert backend_id_for_path(ctx, "/gcs/dir/x") == "path_gcs"
    assert backend_id_for_path(ctx, "/gh/repo/x") == "github_connector"
    assert backend_id_for_path(ctx, "/unmounted/x") is None
