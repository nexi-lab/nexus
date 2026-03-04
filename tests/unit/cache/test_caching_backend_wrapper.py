"""Dedicated CachingBackendWrapper test file (Issue #1524, #2362).

Tests construction, L1 cache hit/miss, cache invalidation on write,
describe() chain introspection, CacheStrategy enum, CacheWrapperConfig
defaults, and WrapperMetrics integration.
"""

from unittest.mock import MagicMock

import pytest

from nexus.backends.wrappers.caching import (
    CacheStrategy,
    CacheWrapperConfig,
    CachingBackendWrapper,
)
from nexus.core.object_store import WriteResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_backend(name: str = "mock") -> MagicMock:
    """Create a mock Backend with standard attributes."""
    backend = MagicMock()
    backend.name = name
    backend.describe.return_value = name
    return backend


# ---------------------------------------------------------------------------
# Construction tests
# ---------------------------------------------------------------------------


class TestConstruction:
    """CachingBackendWrapper can be created with and without a cache_store."""

    def test_construction_without_cache_store(self) -> None:
        """Wrapper initialises with inner backend only (no L2)."""
        inner = _make_mock_backend()
        wrapper = CachingBackendWrapper(inner=inner)
        assert wrapper._cache_store is None
        assert wrapper._config.strategy == CacheStrategy.WRITE_AROUND

    def test_construction_with_cache_store(self) -> None:
        """Wrapper initialises with an L2 cache store."""
        inner = _make_mock_backend()
        cache_store = MagicMock()
        wrapper = CachingBackendWrapper(inner=inner, cache_store=cache_store)
        assert wrapper._cache_store is cache_store

    def test_construction_with_custom_config(self) -> None:
        """Custom CacheWrapperConfig is respected."""
        inner = _make_mock_backend()
        config = CacheWrapperConfig(
            strategy=CacheStrategy.WRITE_THROUGH,
            l1_max_size_mb=64,
            l2_ttl_seconds=1800,
        )
        wrapper = CachingBackendWrapper(inner=inner, config=config)
        assert wrapper._config.strategy == CacheStrategy.WRITE_THROUGH
        assert wrapper._config.l1_max_size_mb == 64
        assert wrapper._config.l2_ttl_seconds == 1800


# ---------------------------------------------------------------------------
# L1 cache hit / miss tests
# ---------------------------------------------------------------------------


class TestL1CacheHitMiss:
    """L1 in-memory cache stores content on read and returns it on re-read."""

    def test_l1_cache_miss_delegates_to_inner(self) -> None:
        """First read_content delegates to inner backend (L1 miss)."""
        inner = _make_mock_backend()
        inner.read_content.return_value = b"file-data"
        wrapper = CachingBackendWrapper(inner=inner)

        response = wrapper.read_content("hash123")
        assert response == b"file-data"
        inner.read_content.assert_called_once()

    def test_l1_cache_hit_avoids_inner(self) -> None:
        """Second read_content with same hash returns from L1 (no inner call)."""
        inner = _make_mock_backend()
        inner.read_content.return_value = b"file-data"
        wrapper = CachingBackendWrapper(inner=inner)

        # First read — populates L1
        wrapper.read_content("hash123")
        inner.read_content.reset_mock()

        # Second read — should hit L1
        response = wrapper.read_content("hash123")
        assert response == b"file-data"
        inner.read_content.assert_not_called()

    def test_l1_miss_increments_miss_counter(self) -> None:
        """L1 miss is tracked in stats."""
        inner = _make_mock_backend()
        inner.read_content.return_value = b"data"
        wrapper = CachingBackendWrapper(inner=inner)

        wrapper.read_content("hash_a")
        stats = wrapper.get_cache_stats()
        assert stats["l1_misses"] >= 1

    def test_l1_hit_increments_hit_counter(self) -> None:
        """L1 hit is tracked in stats."""
        inner = _make_mock_backend()
        inner.read_content.return_value = b"data"
        wrapper = CachingBackendWrapper(inner=inner)

        wrapper.read_content("hash_b")
        wrapper.read_content("hash_b")
        stats = wrapper.get_cache_stats()
        assert stats["l1_hits"] >= 1


# ---------------------------------------------------------------------------
# Cache invalidation on write
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    """write_content invalidates L1 cache for WRITE_AROUND strategy."""

    def test_write_around_invalidates_l1(self) -> None:
        """After write_content, cached content for that hash is evicted."""
        inner = _make_mock_backend()
        inner.read_content.return_value = b"original"
        inner.write_content.return_value = WriteResult(content_hash="hash_x")

        config = CacheWrapperConfig(strategy=CacheStrategy.WRITE_AROUND)
        wrapper = CachingBackendWrapper(inner=inner, config=config)

        # Populate L1
        wrapper.read_content("hash_x")
        inner.read_content.reset_mock()

        # Write invalidates
        wrapper.write_content(b"new-data")

        # Next read should go to inner (L1 was invalidated)
        inner.read_content.return_value = b"new-data"
        response = wrapper.read_content("hash_x")
        inner.read_content.assert_called_once()
        assert response == b"new-data"

    def test_write_through_populates_l1(self) -> None:
        """WRITE_THROUGH strategy populates L1 on write."""
        inner = _make_mock_backend()
        inner.write_content.return_value = WriteResult(content_hash="hash_y")

        config = CacheWrapperConfig(strategy=CacheStrategy.WRITE_THROUGH)
        wrapper = CachingBackendWrapper(inner=inner, config=config)

        wrapper.write_content(b"content-data")

        # L1 should have the content now; read should not call inner
        response = wrapper.read_content("hash_y")
        assert response == b"content-data"
        inner.read_content.assert_not_called()


# ---------------------------------------------------------------------------
# describe() chain introspection
# ---------------------------------------------------------------------------


class TestDescribe:
    """describe() returns the correct chain description."""

    def test_describe_returns_chain(self) -> None:
        """describe() prepends 'cache' to inner's describe()."""
        inner = _make_mock_backend(name="local")
        inner.describe.return_value = "local"
        wrapper = CachingBackendWrapper(inner=inner)
        assert wrapper.describe() == "cache \u2192 local"

    def test_name_wraps_inner(self) -> None:
        """name property wraps inner backend name."""
        inner = _make_mock_backend(name="s3")
        wrapper = CachingBackendWrapper(inner=inner)
        assert wrapper.name == "cached(s3)"


# ---------------------------------------------------------------------------
# CacheStrategy enum
# ---------------------------------------------------------------------------


class TestCacheStrategyEnum:
    """CacheStrategy enum has the expected members."""

    def test_write_around_exists(self) -> None:
        assert CacheStrategy.WRITE_AROUND.value == "write_around"

    def test_write_through_exists(self) -> None:
        assert CacheStrategy.WRITE_THROUGH.value == "write_through"

    def test_enum_has_exactly_two_members(self) -> None:
        assert len(CacheStrategy) == 2


# ---------------------------------------------------------------------------
# CacheWrapperConfig defaults
# ---------------------------------------------------------------------------


class TestCacheWrapperConfigDefaults:
    """CacheWrapperConfig frozen dataclass has sensible defaults."""

    def test_default_strategy(self) -> None:
        config = CacheWrapperConfig()
        assert config.strategy == CacheStrategy.WRITE_AROUND

    def test_default_l1_max_size_mb(self) -> None:
        config = CacheWrapperConfig()
        assert config.l1_max_size_mb == 128

    def test_default_l1_compression_threshold(self) -> None:
        config = CacheWrapperConfig()
        assert config.l1_compression_threshold == 1024

    def test_default_l2_enabled(self) -> None:
        config = CacheWrapperConfig()
        assert config.l2_enabled is True

    def test_default_l2_ttl_seconds(self) -> None:
        config = CacheWrapperConfig()
        assert config.l2_ttl_seconds == 3600

    def test_default_l2_key_prefix(self) -> None:
        config = CacheWrapperConfig()
        assert config.l2_key_prefix == "cbw"

    def test_default_metrics_enabled(self) -> None:
        config = CacheWrapperConfig()
        assert config.metrics_enabled is True

    def test_config_is_frozen(self) -> None:
        """CacheWrapperConfig should be immutable."""
        config = CacheWrapperConfig()
        with pytest.raises(AttributeError):
            config.strategy = CacheStrategy.WRITE_THROUGH  # type: ignore[misc]


# ---------------------------------------------------------------------------
# L2 write-populate-only and stats shape (Issue #2362)
# ---------------------------------------------------------------------------


class TestL2WritePopulateOnly:
    """Verify L2 is write-populate-only (no sync reads)."""

    def test_stats_show_l2_mode(self) -> None:
        """get_cache_stats() includes l2_mode: write-populate-only."""
        inner = _make_mock_backend()
        wrapper = CachingBackendWrapper(inner=inner)
        stats = wrapper.get_cache_stats()
        assert stats["l2_mode"] == "write-populate-only"

    def test_stats_no_l2_hit_miss_counters(self) -> None:
        """Stats no longer include l2_hits or l2_misses (L2 is write-only)."""
        inner = _make_mock_backend()
        wrapper = CachingBackendWrapper(inner=inner)
        stats = wrapper.get_cache_stats()
        assert "l2_hits" not in stats
        assert "l2_misses" not in stats

    def test_clear_cache_resets_all_counters(self) -> None:
        """clear_cache() resets WrapperMetrics counters."""
        inner = _make_mock_backend()
        inner.read_content.return_value = b"data"
        wrapper = CachingBackendWrapper(inner=inner)

        wrapper.read_content("hash_z")
        wrapper.clear_cache()
        stats = wrapper.get_cache_stats()
        assert stats["l1_misses"] == 0
        assert stats["l1_hits"] == 0
