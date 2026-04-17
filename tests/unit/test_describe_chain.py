"""Unit tests for describe() composition chain output (#1449, #1705).

Tests verify the recursive wrapping chain description follows the
format: "layer1 → layer2 → ... → leaf" using unicode arrows.

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping Rule #3
"""

from unittest.mock import MagicMock

import pytest

# Reusable test key for EncryptedStorage tests
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

from nexus.backends.base.backend import Backend
from nexus.backends.storage.delegating import DelegatingBackend
from nexus.backends.wrappers.caching import (
    CacheWrapperConfig,
    CachingBackendWrapper,
)
from nexus.backends.wrappers.compressed import (
    CompressedStorage,
    CompressedStorageConfig,
    is_zstd_available,
)
from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig
from nexus.backends.wrappers.logging import LoggingBackendWrapper
from nexus.contracts.describable import Describable

_skip_no_zstd = pytest.mark.skipif(
    not is_zstd_available(),
    reason="zstd not available (requires Python 3.14+ stdlib compression.zstd)",
)

_TEST_KEY = AESGCMSIV.generate_key(bit_length=256)


def _make_leaf(name: str = "local") -> Backend:
    """Create a mock leaf backend with the given name."""
    mock = MagicMock(spec=Backend)
    mock.name = name
    mock.describe.return_value = name
    return mock


class TestLeafBackendDescribe:
    """Leaf backends should return their name from describe()."""

    def test_leaf_returns_name(self) -> None:
        leaf = _make_leaf("s3")
        assert leaf.describe() == "s3"

    def test_leaf_returns_local(self) -> None:
        leaf = _make_leaf("local")
        assert leaf.describe() == "local"


class TestSingleWrapperDescribe:
    """Single wrapper should prepend its layer name."""

    def test_caching_wrapper(self) -> None:
        leaf = _make_leaf("local")
        config = CacheWrapperConfig(l2_enabled=False, metrics_enabled=False)
        wrapper = CachingBackendWrapper(inner=leaf, config=config)
        assert wrapper.describe() == "cache → local"

    def test_logging_wrapper(self) -> None:
        leaf = _make_leaf("s3")
        wrapper = LoggingBackendWrapper(inner=leaf)
        assert wrapper.describe() == "logging → s3"

    def test_encrypted_wrapper(self) -> None:
        leaf = _make_leaf("local")
        config = EncryptedStorageConfig(key=_TEST_KEY, metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "encrypt(AES-256-GCM-SIV) → local"

    @_skip_no_zstd
    def test_compressed_wrapper(self) -> None:
        leaf = _make_leaf("s3")
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "compress(zstd) → s3"


class TestTwoDeepChainDescribe:
    """2-deep chain should show all layers in order."""

    def test_cache_then_logging(self) -> None:
        leaf = _make_leaf("s3")
        logged = LoggingBackendWrapper(inner=leaf)
        config = CacheWrapperConfig(l2_enabled=False, metrics_enabled=False)
        cached = CachingBackendWrapper(inner=logged, config=config)
        assert cached.describe() == "cache → logging → s3"

    def test_logging_then_cache(self) -> None:
        """Order matters — reversed chain should produce reversed description."""
        leaf = _make_leaf("gcs")
        config = CacheWrapperConfig(l2_enabled=False, metrics_enabled=False)
        cached = CachingBackendWrapper(inner=leaf, config=config)
        logged = LoggingBackendWrapper(inner=cached)
        assert logged.describe() == "logging → cache → gcs"


class TestDeepChainDescribe:
    """3+ deep chains should compose recursively."""

    def test_three_deep_chain(self) -> None:
        leaf = _make_leaf("local")
        logged1 = LoggingBackendWrapper(inner=leaf)
        logged2 = LoggingBackendWrapper(inner=logged1)
        config = CacheWrapperConfig(l2_enabled=False, metrics_enabled=False)
        cached = CachingBackendWrapper(inner=logged2, config=config)
        assert cached.describe() == "cache → logging → logging → local"

    def test_same_wrapper_stacked(self) -> None:
        """Same wrapper type stacked should repeat in description."""
        leaf = _make_leaf("s3")
        config = CacheWrapperConfig(l2_enabled=False, metrics_enabled=False)
        inner_cache = CachingBackendWrapper(inner=leaf, config=config)
        outer_cache = CachingBackendWrapper(inner=inner_cache, config=config)
        assert outer_cache.describe() == "cache → cache → s3"

    @_skip_no_zstd
    def test_full_production_chain(self) -> None:
        """Recommended production chain: cache → compress → encrypt → leaf."""
        leaf = _make_leaf("s3")
        enc_config = EncryptedStorageConfig(key=_TEST_KEY, metrics_enabled=False)
        encrypted = EncryptedStorage(inner=leaf, config=enc_config)
        cmp_config = CompressedStorageConfig(metrics_enabled=False)
        compressed = CompressedStorage(inner=encrypted, config=cmp_config)
        cache_config = CacheWrapperConfig(l2_enabled=False, metrics_enabled=False)
        cached = CachingBackendWrapper(inner=compressed, config=cache_config)
        assert cached.describe() == "cache → compress(zstd) → encrypt(AES-256-GCM-SIV) → s3"


class TestDescribableProtocol:
    """Verify structural subtyping with Describable protocol."""

    def test_leaf_is_describable(self) -> None:
        leaf = _make_leaf("local")
        assert isinstance(leaf, Describable)

    def test_caching_wrapper_is_describable(self) -> None:
        leaf = _make_leaf("local")
        config = CacheWrapperConfig(l2_enabled=False, metrics_enabled=False)
        wrapper = CachingBackendWrapper(inner=leaf, config=config)
        assert isinstance(wrapper, Describable)

    def test_logging_wrapper_is_describable(self) -> None:
        leaf = _make_leaf("local")
        wrapper = LoggingBackendWrapper(inner=leaf)
        assert isinstance(wrapper, Describable)

    def test_delegating_backend_is_describable(self) -> None:
        leaf = _make_leaf("local")
        wrapper = DelegatingBackend(inner=leaf)
        assert isinstance(wrapper, Describable)

    def test_encrypted_wrapper_is_describable(self) -> None:
        leaf = _make_leaf("local")
        config = EncryptedStorageConfig(key=_TEST_KEY, metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert isinstance(wrapper, Describable)

    @_skip_no_zstd
    def test_compressed_wrapper_is_describable(self) -> None:
        leaf = _make_leaf("local")
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert isinstance(wrapper, Describable)


class TestDescribeUnicodeArrow:
    """Verify the unicode arrow separator convention."""

    @pytest.mark.parametrize(
        "depth",
        [1, 2, 3],
    )
    def test_arrow_count_matches_depth(self, depth: int) -> None:
        """Number of arrows should equal the number of layers."""
        leaf = _make_leaf("leaf")
        current: Backend = leaf
        for _ in range(depth):
            current = LoggingBackendWrapper(inner=current)
        description = current.describe()
        assert description.count("→") == depth
        assert description.endswith("leaf")
