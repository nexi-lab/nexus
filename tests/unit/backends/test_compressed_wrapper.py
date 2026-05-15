"""Unit tests for CompressedStorage wrapper (#1705, #2077).

TDD: Tests written BEFORE implementation to drive the interface design.

Tests verify:
1. describe() chain output
2. Compress → decompress roundtrip correctness
3. CAS dedup preservation (zstd determinism)
4. Below-threshold passthrough (content < min_size)
5. Negative compression ratio handling
6. Empty content edge case
7. zstd unavailability handling
8. Delegation of non-content ops
9. Config validation
10. batch_read_content per-item decompression
11. Compression failure fallback (#2077, Issue 10)

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.wrappers.compressed import is_zstd_available
from nexus.contracts.describable import Describable
from nexus.core.object_store import WriteResult
from tests.unit.backends.wrapper_test_helpers import make_leaf, make_storage_mock

pytestmark = pytest.mark.skipif(
    not is_zstd_available(),
    reason="zstd not available (requires Python 3.14+ stdlib compression.zstd)",
)

# ---------------------------------------------------------------------------
# describe() Tests
# ---------------------------------------------------------------------------


class TestCompressedDescribe:
    """describe() should prepend compression layer info."""

    def test_single_wrapper(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        leaf = make_leaf("local")
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "compress(zstd) → local"

    def test_chain_with_logging(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        leaf = make_leaf("s3")
        leaf.describe.return_value = "logging → s3"
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "compress(zstd) → logging → s3"

    def test_is_describable(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        leaf = make_leaf("local")
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert isinstance(wrapper, Describable)


# ---------------------------------------------------------------------------
# Roundtrip Tests
# ---------------------------------------------------------------------------


class TestCompressedRoundtrip:
    """Write + read should return identical content."""

    def test_basic_roundtrip(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        plaintext = b"hello world " * 100  # Compressible content
        write_resp = wrapper.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)

        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == plaintext

    def test_binary_roundtrip(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        plaintext = bytes(range(256)) * 100
        write_resp = wrapper.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)

        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == plaintext

    def test_large_content_roundtrip(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        plaintext = b"A" * (1024 * 1024)  # 1MB highly compressible
        write_resp = wrapper.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)

        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == plaintext


# ---------------------------------------------------------------------------
# CAS Dedup Tests (zstd determinism)
# ---------------------------------------------------------------------------


class TestCompressedCASDedup:
    """Same content + same level should produce same compressed output → same hash."""

    def test_deterministic_compression(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        content = b"deterministic content " * 100
        hash1 = wrapper.write_content(content).content_id
        hash2 = wrapper.write_content(content).content_id
        assert hash1 == hash2, "zstd should produce identical output for identical input"


# ---------------------------------------------------------------------------
# Below-Threshold Tests
# ---------------------------------------------------------------------------


class TestCompressedThreshold:
    """Content below min_size should be stored uncompressed."""

    def test_below_threshold_passthrough(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=1024, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        small_content = b"tiny"  # 4 bytes, below 1024 threshold
        write_resp = wrapper.write_content(small_content)
        assert isinstance(write_resp, WriteResult)

        # Read should return original content
        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == small_content

    def test_above_threshold_compressed(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=64, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        # Content above threshold — should be compressed
        large_content = b"compressible " * 100  # 1300 bytes
        write_resp = wrapper.write_content(large_content)
        assert isinstance(write_resp, WriteResult)

        # The stored content should be smaller than original
        stored = storage[write_resp.content_id]
        assert len(stored) < len(large_content)

        # Read should return original
        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == large_content


# ---------------------------------------------------------------------------
# Negative Compression Ratio Tests
# ---------------------------------------------------------------------------


class TestCompressedNegativeRatio:
    """Pre-compressed or random content should be stored uncompressed."""

    def test_random_content_passthrough(self) -> None:
        import os

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        # Random data won't compress well
        random_data = os.urandom(1024)
        write_resp = wrapper.write_content(random_data)
        assert isinstance(write_resp, WriteResult)

        # Read should return original regardless of whether compression was skipped
        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == random_data


# ---------------------------------------------------------------------------
# Empty Content Edge Case
# ---------------------------------------------------------------------------


class TestCompressedEmptyContent:
    """Empty content should pass through without compression."""

    def test_empty_roundtrip(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        write_resp = wrapper.write_content(b"")
        assert isinstance(write_resp, WriteResult)

        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == b""


# ---------------------------------------------------------------------------
# Delegation Tests
# ---------------------------------------------------------------------------


class TestCompressedDelegation:
    """Non-content ops should pass through to inner backend."""

    def test_mkdir_delegates(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        leaf = make_leaf("local")
        leaf.mkdir.return_value = None
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)

        result = wrapper.mkdir("/test", parents=True)
        leaf.mkdir.assert_called_once()
        assert result is None

    def test_delete_delegates(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        leaf = make_leaf("local")
        leaf.delete_content.return_value = None
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)

        result = wrapper.delete_content("hash123")
        leaf.delete_content.assert_called_once()
        assert result is None


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------


class TestCompressedConfig:
    """Config validation."""

    def test_default_config(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorageConfig

        config = CompressedStorageConfig()
        assert config.level == 3
        assert config.min_size == 64
        assert config.metrics_enabled is True

    def test_custom_level(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorageConfig

        config = CompressedStorageConfig(level=10)
        assert config.level == 10

    def test_invalid_level_raises(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorageConfig

        with pytest.raises(ValueError, match="level"):
            CompressedStorageConfig(level=0)

        with pytest.raises(ValueError, match="level"):
            CompressedStorageConfig(level=23)


# ---------------------------------------------------------------------------
# Batch Operation Tests
# ---------------------------------------------------------------------------


class TestCompressedBatch:
    """batch_read_content should decompress each item individually."""

    def test_batch_read_decompresses_all(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        # Write 3 compressible items
        content_a = b"alpha " * 100
        content_b = b"beta " * 100
        content_c = b"gamma " * 100

        h1 = wrapper.write_content(content_a).content_id
        h2 = wrapper.write_content(content_b).content_id
        h3 = wrapper.write_content(content_c).content_id

        # Batch read
        results = wrapper.batch_read_content([h1, h2, h3])
        assert results[h1] == content_a
        assert results[h2] == content_b
        assert results[h3] == content_c

    def test_batch_read_handles_missing(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        h1 = wrapper.write_content(b"exists " * 100).content_id
        results = wrapper.batch_read_content([h1, "nonexistent"])
        assert results[h1] == b"exists " * 100
        assert results["nonexistent"] is None


# ---------------------------------------------------------------------------
# Compression Failure Fallback Tests (#2077, Issue 10)
# ---------------------------------------------------------------------------


class TestCompressedFailureFallback:
    """Compression failure should fall back to uncompressed storage."""

    def test_compress_failure_stores_uncompressed(self) -> None:
        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

        mock, storage = make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        content = b"this should be stored uncompressed " * 100

        # Replace the cached compressor with a mock that raises on compress.
        # ZstdCompressor.compress is a read-only C slot, so we swap the whole
        # object rather than patching the attribute directly.
        broken_compressor = MagicMock()
        broken_compressor.compress.side_effect = RuntimeError("compressor broken")
        with patch.object(wrapper, "_cached_compressor", broken_compressor):
            write_resp = wrapper.write_content(content)

        # Write should succeed with uncompressed content
        assert isinstance(write_resp, WriteResult)
        stored = storage[write_resp.content_id]
        assert stored == content  # Stored raw, no NEXZ header

        # Read should return original
        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == content
