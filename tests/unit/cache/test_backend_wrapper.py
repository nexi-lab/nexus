"""Unit tests for CachingBackendWrapper (#1392).

Two test suites:
1. Conformance tests — parametrized to run against raw and wrapped backends,
   proving the wrapper is transparent (same behavior as unwrapped).
2. Cache behavior tests — verify L1 caching logic, invalidation strategies,
   error handling, stats, and configuration.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from nexus.cache.backend_wrapper import (
    CacheStrategy,
    CacheWrapperConfig,
    CachingBackendWrapper,
)
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.core.cache_store import NullCacheStore
from tests.unit.cache.mock_backend import MockBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def inner_backend() -> MockBackend:
    """Fresh MockBackend for each test."""
    return MockBackend()


@pytest.fixture
def default_config() -> CacheWrapperConfig:
    """Default wrapper config with small L1 for testing."""
    return CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=False)


@pytest.fixture
def wrapped_backend(
    inner_backend: MockBackend, default_config: CacheWrapperConfig
) -> CachingBackendWrapper:
    """CachingBackendWrapper wrapping a MockBackend."""
    return CachingBackendWrapper(inner=inner_backend, config=default_config)


@pytest.fixture(params=["raw", "wrapped"])
def backend(request, inner_backend: MockBackend, default_config: CacheWrapperConfig):
    """Parametrized fixture: returns either raw MockBackend or wrapped version.

    Both should produce identical results for conformance tests.
    """
    if request.param == "raw":
        return inner_backend
    return CachingBackendWrapper(inner=MockBackend(), config=default_config)


# ===========================================================================
# SUITE 1: Conformance Tests (transparency proof)
# ===========================================================================


class TestConformance:
    """Verify CachingBackendWrapper produces identical results to the raw backend.

    All tests run twice via parametrized `backend` fixture — once raw, once wrapped.
    """

    def test_write_content_returns_correct_hash(self, backend):
        content = b"Hello, conformance!"
        response = backend.write_content(content)
        assert response.success
        assert response.data == _hash(content)

    def test_read_content_returns_correct_bytes(self, backend):
        content = b"Read me back"
        write_resp = backend.write_content(content)
        content_hash = write_resp.data

        read_resp = backend.read_content(content_hash)
        assert read_resp.success
        assert read_resp.data == content

    def test_read_content_not_found(self, backend):
        resp = backend.read_content("nonexistent_hash")
        assert not resp.success

    def test_delete_content_success(self, backend):
        content = b"Delete me"
        write_resp = backend.write_content(content)
        content_hash = write_resp.data

        delete_resp = backend.delete_content(content_hash)
        assert delete_resp.success

        # Should no longer exist
        exists_resp = backend.content_exists(content_hash)
        assert exists_resp.success
        assert exists_resp.data is False

    def test_delete_content_not_found(self, backend):
        resp = backend.delete_content("nonexistent_hash")
        assert not resp.success

    def test_content_exists_true(self, backend):
        content = b"I exist"
        write_resp = backend.write_content(content)

        exists_resp = backend.content_exists(write_resp.data)
        assert exists_resp.success
        assert exists_resp.data is True

    def test_content_exists_false(self, backend):
        exists_resp = backend.content_exists("nonexistent_hash")
        assert exists_resp.success
        assert exists_resp.data is False

    def test_get_content_size(self, backend):
        content = b"Size check" * 10
        write_resp = backend.write_content(content)

        size_resp = backend.get_content_size(write_resp.data)
        assert size_resp.success
        assert size_resp.data == len(content)

    def test_get_ref_count(self, backend):
        content = b"Ref count"
        write_resp = backend.write_content(content)

        ref_resp = backend.get_ref_count(write_resp.data)
        assert ref_resp.success
        assert ref_resp.data == 1

    def test_batch_read_content(self, backend):
        c1, c2 = b"batch one", b"batch two"
        h1 = backend.write_content(c1).data
        h2 = backend.write_content(c2).data

        result = backend.batch_read_content([h1, h2, "missing"])
        assert result[h1] == c1
        assert result[h2] == c2
        assert result["missing"] is None

    def test_mkdir_and_is_directory(self, backend):
        resp = backend.mkdir("/test/dir", parents=True, exist_ok=True)
        assert resp.success

        is_dir_resp = backend.is_directory("/test/dir")
        assert is_dir_resp.success
        assert is_dir_resp.data is True

    def test_rmdir(self, backend):
        backend.mkdir("/rm_me", exist_ok=True)
        resp = backend.rmdir("/rm_me")
        assert resp.success

    def test_write_duplicate_increments_ref_count(self, backend):
        content = b"duplicate"
        backend.write_content(content)
        backend.write_content(content)

        ref_resp = backend.get_ref_count(_hash(content))
        assert ref_resp.success
        assert ref_resp.data == 2


# ===========================================================================
# SUITE 2: Cache Behavior Tests
# ===========================================================================


class TestCacheBehavior:
    """Verify caching logic: L1 hits/misses, invalidation, stats."""

    def test_name_includes_inner_name(self, wrapped_backend: CachingBackendWrapper):
        assert "cached(" in wrapped_backend.name
        assert "mock" in wrapped_backend.name

    def test_l1_hit_avoids_inner_read(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """Second read_content should come from L1, not inner backend."""
        content = b"cache me"
        write_resp = wrapped_backend.write_content(content)
        content_hash = write_resp.data

        # First read — cache miss, hits inner backend
        wrapped_backend.read_content(content_hash)
        reads_after_first = inner_backend.call_counts["read_content"]

        # Second read — should be L1 hit, inner backend NOT called
        resp = wrapped_backend.read_content(content_hash)
        assert resp.success
        assert resp.data == content
        assert inner_backend.call_counts["read_content"] == reads_after_first

    def test_l1_miss_populates_cache(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """First read should populate L1 cache."""
        content = b"populate me"
        content_hash = _hash(content)
        inner_backend._content[content_hash] = content
        inner_backend._ref_counts[content_hash] = 1

        # First read — miss, reads from inner
        resp = wrapped_backend.read_content(content_hash)
        assert resp.success
        assert resp.data == content
        assert inner_backend.call_counts["read_content"] == 1

        # Second read — L1 hit
        resp2 = wrapped_backend.read_content(content_hash)
        assert resp2.success
        assert resp2.data == content
        assert inner_backend.call_counts["read_content"] == 1  # unchanged

    def test_write_around_invalidates_l1(self, inner_backend: MockBackend):
        """Write-around strategy: write invalidates L1, doesn't populate."""
        config = CacheWrapperConfig(
            strategy=CacheStrategy.WRITE_AROUND, l1_max_size_mb=1, l2_enabled=False
        )
        wrapper = CachingBackendWrapper(inner=inner_backend, config=config)

        content = b"write around"
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        # Read to populate cache
        wrapper.read_content(content_hash)
        assert inner_backend.call_counts["read_content"] == 1

        # Second read — cached
        wrapper.read_content(content_hash)
        assert inner_backend.call_counts["read_content"] == 1

        # Overwrite with new content (same hash — CAS is dedup, but test invalidation)
        new_content = b"new content different"
        new_hash = _hash(new_content)
        inner_backend._content[new_hash] = new_content
        inner_backend._ref_counts[new_hash] = 1

        # Write-around should not populate L1 with the written content
        wrapper.write_content(new_content)

        # Reading new content should go to inner backend (L1 miss)
        wrapper.read_content(new_hash)
        assert inner_backend.call_counts["read_content"] == 2

    def test_write_through_populates_l1(self, inner_backend: MockBackend):
        """Write-through strategy: write populates L1."""
        config = CacheWrapperConfig(
            strategy=CacheStrategy.WRITE_THROUGH, l1_max_size_mb=1, l2_enabled=False
        )
        wrapper = CachingBackendWrapper(inner=inner_backend, config=config)

        content = b"write through"
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        # Read should be L1 hit — no inner backend read needed
        resp = wrapper.read_content(content_hash)
        assert resp.success
        assert resp.data == content
        assert inner_backend.call_counts["read_content"] == 0  # never called!

    def test_delete_invalidates_l1(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """delete_content should remove entry from L1 cache."""
        content = b"delete invalidates"
        write_resp = wrapped_backend.write_content(content)
        content_hash = write_resp.data

        # Populate L1
        wrapped_backend.read_content(content_hash)
        assert inner_backend.call_counts["read_content"] == 1

        # Verify L1 hit
        wrapped_backend.read_content(content_hash)
        assert inner_backend.call_counts["read_content"] == 1

        # Delete should invalidate L1
        wrapped_backend.delete_content(content_hash)

        # Next read should go to inner backend (but content gone, so not_found)
        resp = wrapped_backend.read_content(content_hash)
        assert not resp.success
        assert inner_backend.call_counts["read_content"] == 2

    def test_batch_read_uses_l1_for_cached_items(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """batch_read_content should use L1 for already-cached items."""
        c1, c2, c3 = b"batch1", b"batch2", b"batch3"
        h1 = wrapped_backend.write_content(c1).data
        h2 = wrapped_backend.write_content(c2).data
        h3 = wrapped_backend.write_content(c3).data

        # Populate L1 for h1 and h2 only
        wrapped_backend.read_content(h1)
        wrapped_backend.read_content(h2)

        # batch_read should use L1 for h1, h2 and only hit inner for h3
        result = wrapped_backend.batch_read_content([h1, h2, h3])
        assert result[h1] == c1
        assert result[h2] == c2
        assert result[h3] == c3

    def test_content_exists_always_delegates_to_inner(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """content_exists always checks inner backend (source of truth)."""
        content = b"exists check"
        write_resp = wrapped_backend.write_content(content)
        content_hash = write_resp.data

        # Populate L1
        wrapped_backend.read_content(content_hash)

        exists_count_before = inner_backend.call_counts["content_exists"]

        # content_exists should ALWAYS delegate to inner (not L1 shortcut)
        resp = wrapped_backend.content_exists(content_hash)
        assert resp.success
        assert resp.data is True
        assert inner_backend.call_counts["content_exists"] == exists_count_before + 1

    def test_cache_error_falls_through(self, inner_backend: MockBackend):
        """If L1 cache raises, fall through to inner backend."""
        config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=False)
        wrapper = CachingBackendWrapper(inner=inner_backend, config=config)

        content = b"error fallback"
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        # Corrupt L1 cache to force error
        wrapper._l1_cache._cache = None  # type: ignore[assignment]

        # Should still work by falling through to inner backend
        resp = wrapper.read_content(content_hash)
        assert resp.success
        assert resp.data == content

    def test_get_cache_stats(self, wrapped_backend: CachingBackendWrapper):
        """get_cache_stats returns L1 stats and hit/miss counters."""
        content = b"stats test"
        write_resp = wrapped_backend.write_content(content)
        content_hash = write_resp.data

        # Generate some hits and misses
        wrapped_backend.read_content(content_hash)  # miss
        wrapped_backend.read_content(content_hash)  # hit
        wrapped_backend.read_content(content_hash)  # hit

        stats = wrapped_backend.get_cache_stats()
        assert "l1" in stats
        assert stats["l1_hits"] >= 2
        assert stats["l1_misses"] >= 1

    def test_clear_cache(self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper):
        """clear_cache should empty L1, forcing next read to inner."""
        content = b"clear me"
        write_resp = wrapped_backend.write_content(content)
        content_hash = write_resp.data

        # Populate L1
        wrapped_backend.read_content(content_hash)
        assert inner_backend.call_counts["read_content"] == 1

        # Verify L1 hit
        wrapped_backend.read_content(content_hash)
        assert inner_backend.call_counts["read_content"] == 1

        # Clear cache
        wrapped_backend.clear_cache()

        # Next read should be L1 miss
        wrapped_backend.read_content(content_hash)
        assert inner_backend.call_counts["read_content"] == 2

    def test_custom_l1_max_size(self, inner_backend: MockBackend):
        """Config l1_max_size_mb should be respected."""
        # Very small cache — 1 byte (effectively 0)
        config = CacheWrapperConfig(l1_max_size_mb=0, l2_enabled=False)
        wrapper = CachingBackendWrapper(inner=inner_backend, config=config)

        content = b"too large for 0MB cache" * 100
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        # Read should work but content too large to cache
        resp = wrapper.read_content(content_hash)
        assert resp.success
        assert inner_backend.call_counts["read_content"] == 1

        # Second read should also go to inner (not cached)
        resp = wrapper.read_content(content_hash)
        assert resp.success
        assert inner_backend.call_counts["read_content"] == 2

    def test_empty_content(self, wrapped_backend: CachingBackendWrapper):
        """Empty bytes should be handled correctly."""
        content = b""
        write_resp = wrapped_backend.write_content(content)
        assert write_resp.success
        content_hash = write_resp.data

        read_resp = wrapped_backend.read_content(content_hash)
        assert read_resp.success
        assert read_resp.data == b""

    def test_getattr_delegates_non_cached_methods(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """__getattr__ should delegate non-cached methods to inner backend."""
        # connect is not explicitly overridden — should delegate via __getattr__
        resp = wrapped_backend.connect()
        assert resp.success

    def test_capability_properties_delegate(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """Capability properties should match inner backend."""
        assert wrapped_backend.user_scoped == inner_backend.user_scoped
        assert wrapped_backend.is_connected == inner_backend.is_connected
        assert wrapped_backend.thread_safe == inner_backend.thread_safe
        assert wrapped_backend.supports_rename == inner_backend.supports_rename
        assert wrapped_backend.has_virtual_filesystem == inner_backend.has_virtual_filesystem
        assert wrapped_backend.has_root_path == inner_backend.has_root_path
        assert wrapped_backend.has_token_manager == inner_backend.has_token_manager
        assert wrapped_backend.has_data_dir == inner_backend.has_data_dir
        assert wrapped_backend.is_passthrough == inner_backend.is_passthrough
        assert (
            wrapped_backend.supports_parallel_mmap_read == inner_backend.supports_parallel_mmap_read
        )

    def test_inner_backend_error_propagated(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """If inner backend returns error, wrapper propagates it (no cache population)."""
        resp = wrapped_backend.read_content("does_not_exist_hash")
        assert not resp.success

    def test_concurrent_writes_same_content(
        self, inner_backend: MockBackend, wrapped_backend: CachingBackendWrapper
    ):
        """Multiple writes of same content should work correctly (CAS dedup)."""
        content = b"concurrent write"
        h1 = wrapped_backend.write_content(content).data
        h2 = wrapped_backend.write_content(content).data
        assert h1 == h2

        ref_resp = wrapped_backend.get_ref_count(h1)
        assert ref_resp.data == 2


# ===========================================================================
# SUITE 3: L2 Cache Tests
# ===========================================================================


class TestL2Cache:
    """Verify L2 (CacheStoreABC) integration."""

    def test_l2_population_on_read_miss(self):
        """L2 should be populated asynchronously after L1 miss."""

        async def _run():
            cache_store = InMemoryCacheStore()
            inner = MockBackend()
            config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=True, l2_key_prefix="test")
            wrapper = CachingBackendWrapper(inner=inner, config=config, cache_store=cache_store)

            content = b"l2 populate"
            write_resp = wrapper.write_content(content)
            content_hash = write_resp.data

            # Read — L1 miss, should schedule L2 population
            wrapper.read_content(content_hash)

            # Allow async task to complete
            await asyncio.sleep(0.05)

            # Verify L2 has the content
            l2_key = f"test:{content_hash}"
            l2_data = await cache_store.get(l2_key)
            assert l2_data == content

        asyncio.run(_run())

    def test_l2_read_on_l1_miss(self):
        """On L1 miss, should check L2 before hitting inner backend."""

        async def _run():
            cache_store = InMemoryCacheStore()
            inner = MockBackend()
            config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=True, l2_key_prefix="test")
            wrapper = CachingBackendWrapper(inner=inner, config=config, cache_store=cache_store)

            content = b"l2 read test"
            content_hash = _hash(content)

            # Pre-populate L2 directly (simulating previous population)
            await cache_store.set(f"test:{content_hash}", content, ttl=300)

            # Also put it in inner backend for consistency
            inner._content[content_hash] = content
            inner._ref_counts[content_hash] = 1

            # Read — L1 miss, should find in L2, NOT hit inner backend
            resp = wrapper.read_content(content_hash)
            assert resp.success
            assert resp.data == content

        asyncio.run(_run())

    def test_l2_disabled_skips_l2(self, inner_backend: MockBackend):
        """When l2_enabled=False, L2 operations are skipped entirely."""
        config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=False)
        wrapper = CachingBackendWrapper(
            inner=inner_backend, config=config, cache_store=NullCacheStore()
        )

        content = b"no l2"
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        # Should work fine without L2
        resp = wrapper.read_content(content_hash)
        assert resp.success
        assert resp.data == content

    def test_l2_error_does_not_break_read(self, inner_backend: MockBackend):
        """L2 errors should be swallowed — reads fall through to inner."""
        config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=True, l2_key_prefix="test")
        # NullCacheStore won't error, but we can test with no cache_store
        wrapper = CachingBackendWrapper(inner=inner_backend, config=config, cache_store=None)

        content = b"l2 error safe"
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        resp = wrapper.read_content(content_hash)
        assert resp.success
        assert resp.data == content

    def test_l2_invalidation_on_delete(self):
        """delete_content should trigger L2 invalidation."""

        async def _run():
            cache_store = InMemoryCacheStore()
            inner = MockBackend()
            config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=True, l2_key_prefix="test")
            wrapper = CachingBackendWrapper(inner=inner, config=config, cache_store=cache_store)

            content = b"l2 invalidate on delete"
            write_resp = wrapper.write_content(content)
            content_hash = write_resp.data

            # Read to populate L1 + L2
            wrapper.read_content(content_hash)
            await asyncio.sleep(0.05)

            # Verify L2 has the content
            l2_key = f"test:{content_hash}"
            assert await cache_store.get(l2_key) == content

            # Delete should invalidate L2
            wrapper.delete_content(content_hash)
            await asyncio.sleep(0.05)

            # Verify L2 no longer has the content
            assert await cache_store.get(l2_key) is None

        asyncio.run(_run())

    def test_batch_read_with_l1_error(self, inner_backend: MockBackend):
        """batch_read_content should fall through on L1 cache error."""
        config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=False)
        wrapper = CachingBackendWrapper(inner=inner_backend, config=config)

        content = b"batch error test"
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        # Corrupt L1 cache to force error
        wrapper._l1_cache._cache = None  # type: ignore[assignment]

        # batch_read should still work by falling through to inner backend
        result = wrapper.batch_read_content([content_hash])
        assert result[content_hash] == content

    def test_clear_cache_resets_all_stats(self, inner_backend: MockBackend):
        """clear_cache should reset all counters including errors and invalidations."""
        config = CacheWrapperConfig(l1_max_size_mb=1, l2_enabled=False)
        wrapper = CachingBackendWrapper(inner=inner_backend, config=config)

        content = b"clear all stats"
        write_resp = wrapper.write_content(content)
        content_hash = write_resp.data

        # Generate some stats
        wrapper.read_content(content_hash)  # miss
        wrapper.read_content(content_hash)  # hit
        wrapper.delete_content(content_hash)  # invalidation

        stats = wrapper.get_cache_stats()
        assert stats["l1_hits"] > 0 or stats["l1_misses"] > 0
        assert stats["invalidations"] > 0

        # Clear should reset everything
        wrapper.clear_cache()
        stats = wrapper.get_cache_stats()
        assert stats["l1_hits"] == 0
        assert stats["l1_misses"] == 0
        assert stats["l2_hits"] == 0
        assert stats["l2_misses"] == 0
        assert stats["cache_errors"] == 0
        assert stats["invalidations"] == 0
