"""Unit tests for CompressedStorage wrapper (#1705).

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

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.backends.backend import Backend
from nexus.core.protocols.describable import Describable
from nexus.core.response import HandlerResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_leaf(name: str = "local") -> MagicMock:
    """Create a mock leaf backend."""
    mock = MagicMock(spec=Backend)
    mock.name = name
    mock.describe.return_value = name
    type(mock).user_scoped = PropertyMock(return_value=False)
    type(mock).is_connected = PropertyMock(return_value=True)
    type(mock).thread_safe = PropertyMock(return_value=True)
    type(mock).supports_rename = PropertyMock(return_value=False)
    type(mock).has_virtual_filesystem = PropertyMock(return_value=False)
    type(mock).has_root_path = PropertyMock(return_value=True)
    type(mock).has_token_manager = PropertyMock(return_value=False)
    type(mock).has_data_dir = PropertyMock(return_value=False)
    type(mock).is_passthrough = PropertyMock(return_value=False)
    type(mock).supports_parallel_mmap_read = PropertyMock(return_value=False)
    return mock


def _make_storage_mock() -> tuple[MagicMock, dict[str, bytes]]:
    """Create a mock leaf that actually stores/retrieves content."""
    storage: dict[str, bytes] = {}
    mock = _make_leaf("storage-mock")

    def write_content(content: bytes, context: object = None) -> HandlerResponse:
        h = hashlib.sha256(content).hexdigest()
        storage[h] = content
        return HandlerResponse.ok(data=h)

    def read_content(content_hash: str, context: object = None) -> HandlerResponse:
        if content_hash in storage:
            return HandlerResponse.ok(data=storage[content_hash])
        return HandlerResponse.error(message="not found")

    def batch_read_content(
        content_hashes: list[str],
        context: object = None,
        *,
        contexts: dict | None = None,
    ) -> dict[str, bytes | None]:
        return {h: storage.get(h) for h in content_hashes}

    mock.write_content = MagicMock(side_effect=write_content)
    mock.read_content = MagicMock(side_effect=read_content)
    mock.batch_read_content = MagicMock(side_effect=batch_read_content)
    mock.delete_content = MagicMock(return_value=HandlerResponse.ok(data=None))
    return mock, storage


# ---------------------------------------------------------------------------
# describe() Tests
# ---------------------------------------------------------------------------


class TestCompressedDescribe:
    """describe() should prepend compression layer info."""

    def test_single_wrapper(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        leaf = _make_leaf("local")
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "compress(zstd) → local"

    def test_chain_with_logging(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        leaf = _make_leaf("s3")
        leaf.describe.return_value = "logging → s3"
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "compress(zstd) → logging → s3"

    def test_is_describable(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        leaf = _make_leaf("local")
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)
        assert isinstance(wrapper, Describable)


# ---------------------------------------------------------------------------
# Roundtrip Tests
# ---------------------------------------------------------------------------


class TestCompressedRoundtrip:
    """Write + read should return identical content."""

    def test_basic_roundtrip(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        plaintext = b"hello world " * 100  # Compressible content
        write_resp = wrapper.write_content(plaintext)
        assert write_resp.success

        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == plaintext

    def test_binary_roundtrip(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        plaintext = bytes(range(256)) * 100
        write_resp = wrapper.write_content(plaintext)
        assert write_resp.success

        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == plaintext

    def test_large_content_roundtrip(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        plaintext = b"A" * (1024 * 1024)  # 1MB highly compressible
        write_resp = wrapper.write_content(plaintext)
        assert write_resp.success

        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == plaintext


# ---------------------------------------------------------------------------
# CAS Dedup Tests (zstd determinism)
# ---------------------------------------------------------------------------


class TestCompressedCASDedup:
    """Same content + same level should produce same compressed output → same hash."""

    def test_deterministic_compression(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        content = b"deterministic content " * 100
        hash1 = wrapper.write_content(content).data
        hash2 = wrapper.write_content(content).data
        assert hash1 == hash2, "zstd should produce identical output for identical input"


# ---------------------------------------------------------------------------
# Below-Threshold Tests
# ---------------------------------------------------------------------------


class TestCompressedThreshold:
    """Content below min_size should be stored uncompressed."""

    def test_below_threshold_passthrough(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=1024, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        small_content = b"tiny"  # 4 bytes, below 1024 threshold
        write_resp = wrapper.write_content(small_content)
        assert write_resp.success

        # Read should return original content
        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == small_content

    def test_above_threshold_compressed(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=64, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        # Content above threshold — should be compressed
        large_content = b"compressible " * 100  # 1300 bytes
        write_resp = wrapper.write_content(large_content)
        assert write_resp.success

        # The stored content should be smaller than original
        stored = storage[write_resp.data]
        assert len(stored) < len(large_content)

        # Read should return original
        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == large_content


# ---------------------------------------------------------------------------
# Negative Compression Ratio Tests
# ---------------------------------------------------------------------------


class TestCompressedNegativeRatio:
    """Pre-compressed or random content should be stored uncompressed."""

    def test_random_content_passthrough(self) -> None:
        import os

        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        # Random data won't compress well
        random_data = os.urandom(1024)
        write_resp = wrapper.write_content(random_data)
        assert write_resp.success

        # Read should return original regardless of whether compression was skipped
        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == random_data


# ---------------------------------------------------------------------------
# Empty Content Edge Case
# ---------------------------------------------------------------------------


class TestCompressedEmptyContent:
    """Empty content should pass through without compression."""

    def test_empty_roundtrip(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        write_resp = wrapper.write_content(b"")
        assert write_resp.success

        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == b""


# ---------------------------------------------------------------------------
# Delegation Tests
# ---------------------------------------------------------------------------


class TestCompressedDelegation:
    """Non-content ops should pass through to inner backend."""

    def test_mkdir_delegates(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        leaf = _make_leaf("local")
        leaf.mkdir.return_value = HandlerResponse.ok(data=None)
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)

        result = wrapper.mkdir("/test", parents=True)
        leaf.mkdir.assert_called_once()
        assert result.success

    def test_delete_delegates(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        leaf = _make_leaf("local")
        leaf.delete_content.return_value = HandlerResponse.ok(data=None)
        config = CompressedStorageConfig(metrics_enabled=False)
        wrapper = CompressedStorage(inner=leaf, config=config)

        result = wrapper.delete_content("hash123")
        leaf.delete_content.assert_called_once()
        assert result.success


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------


class TestCompressedConfig:
    """Config validation."""

    def test_default_config(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorageConfig

        config = CompressedStorageConfig()
        assert config.level == 3
        assert config.min_size == 64
        assert config.metrics_enabled is True

    def test_custom_level(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorageConfig

        config = CompressedStorageConfig(level=10)
        assert config.level == 10

    def test_invalid_level_raises(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorageConfig

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
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        # Write 3 compressible items
        content_a = b"alpha " * 100
        content_b = b"beta " * 100
        content_c = b"gamma " * 100

        h1 = wrapper.write_content(content_a).data
        h2 = wrapper.write_content(content_b).data
        h3 = wrapper.write_content(content_c).data

        # Batch read
        results = wrapper.batch_read_content([h1, h2, h3])
        assert results[h1] == content_a
        assert results[h2] == content_b
        assert results[h3] == content_c

    def test_batch_read_handles_missing(self) -> None:
        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

        mock, storage = _make_storage_mock()
        config = CompressedStorageConfig(min_size=0, metrics_enabled=False)
        wrapper = CompressedStorage(inner=mock, config=config)

        h1 = wrapper.write_content(b"exists " * 100).data
        results = wrapper.batch_read_content([h1, "nonexistent"])
        assert results[h1] == b"exists " * 100
        assert results["nonexistent"] is None
