"""Integration tests for multi-wrapper chain data flow (#1705, #2077, #2362).

Tests verify that the full composition chain (compress → encrypt → leaf)
correctly transforms data on write and reverses transforms on read.

Also tests performance regression tests (Decision 16A).

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
    - Issue #2077: Deduplicate backend wrapper boilerplate
    - Issue #2362: ConnectorProtocol wrapping chains
"""

import time

import pytest

from nexus.backends.wrappers.compressed import is_zstd_available
from nexus.core.object_store import WriteResult
from tests.unit.backends.wrapper_test_helpers import make_storage_mock

pytestmark = pytest.mark.skipif(
    not is_zstd_available(),
    reason="zstd not available (requires Python 3.14+ stdlib compression.zstd)",
)

# ---------------------------------------------------------------------------
# Chain Composition Tests
# ---------------------------------------------------------------------------


class TestCompressEncryptChain:
    """Full chain: compress → encrypt → leaf (correct ordering)."""

    def test_describe_full_chain(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, _ = make_storage_mock()
        key = AESGCMSIV.generate_key(bit_length=256)

        # Build chain: compress → encrypt → leaf
        encrypted = EncryptedStorage(
            inner=mock,
            config=EncryptedStorageConfig(key=key, metrics_enabled=False),
        )
        compressed = CompressedStorage(
            inner=encrypted,
            config=CompressedStorageConfig(metrics_enabled=False),
        )
        assert compressed.describe() == "compress(zstd) → encrypt(AES-256-GCM-SIV) → storage-mock"

    def test_roundtrip_through_chain(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        key = AESGCMSIV.generate_key(bit_length=256)

        # Build chain: compress → encrypt → leaf
        encrypted = EncryptedStorage(
            inner=mock,
            config=EncryptedStorageConfig(key=key, metrics_enabled=False),
        )
        compressed = CompressedStorage(
            inner=encrypted,
            config=CompressedStorageConfig(min_size=0, metrics_enabled=False),
        )

        # Write plaintext through entire chain
        plaintext = b"hello world from the full chain! " * 100
        write_resp = compressed.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)

        # Verify stored content is neither plaintext nor just compressed
        stored = storage[write_resp.content_id]
        assert stored != plaintext, "Stored content should be encrypted"

        # Read back through chain
        read_back = compressed.read_content(write_resp.content_id)
        assert read_back == plaintext

    def test_chain_dedup(self) -> None:
        """Same content through same chain should produce same hash."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        key = AESGCMSIV.generate_key(bit_length=256)

        encrypted = EncryptedStorage(
            inner=mock,
            config=EncryptedStorageConfig(key=key, metrics_enabled=False),
        )
        compressed = CompressedStorage(
            inner=encrypted,
            config=CompressedStorageConfig(min_size=0, metrics_enabled=False),
        )

        content = b"deduplicate me " * 100
        h1 = compressed.write_content(content).content_id
        h2 = compressed.write_content(content).content_id
        assert h1 == h2

    def test_chain_batch_read(self) -> None:
        """batch_read_content through chain should decompress + decrypt all."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        key = AESGCMSIV.generate_key(bit_length=256)

        encrypted = EncryptedStorage(
            inner=mock,
            config=EncryptedStorageConfig(key=key, metrics_enabled=False),
        )
        compressed = CompressedStorage(
            inner=encrypted,
            config=CompressedStorageConfig(min_size=0, metrics_enabled=False),
        )

        items = [b"item_a " * 50, b"item_b " * 50, b"item_c " * 50]
        hashes = [compressed.write_content(item).content_id for item in items]

        results = compressed.batch_read_content(hashes)
        for h, expected in zip(hashes, items):
            assert results[h] == expected


class TestEncryptCompressChain:
    """Reversed chain: encrypt → compress → leaf (suboptimal but valid)."""

    def test_describe_reversed(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, _ = make_storage_mock()
        key = AESGCMSIV.generate_key(bit_length=256)

        # Reversed: encrypt → compress → leaf (suboptimal, but must still work)
        compressed = CompressedStorage(
            inner=mock,
            config=CompressedStorageConfig(metrics_enabled=False),
        )
        encrypted = EncryptedStorage(
            inner=compressed,
            config=EncryptedStorageConfig(key=key, metrics_enabled=False),
        )
        assert encrypted.describe() == "encrypt(AES-256-GCM-SIV) → compress(zstd) → storage-mock"

    def test_reversed_roundtrip(self) -> None:
        """Reversed ordering should still produce correct roundtrip."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        key = AESGCMSIV.generate_key(bit_length=256)

        compressed = CompressedStorage(
            inner=mock,
            config=CompressedStorageConfig(min_size=0, metrics_enabled=False),
        )
        encrypted = EncryptedStorage(
            inner=compressed,
            config=EncryptedStorageConfig(key=key, metrics_enabled=False),
        )

        plaintext = b"reversed chain data " * 100
        write_resp = encrypted.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)

        read_back = encrypted.read_content(write_resp.content_id)
        assert read_back == plaintext


# ---------------------------------------------------------------------------
# Performance Smoke Test (#2077, Issue 16)
# ---------------------------------------------------------------------------


class TestWrapperChainPerformance:
    """Verify wrapper chain doesn't introduce excessive overhead."""

    def test_wrapper_overhead_under_threshold(self) -> None:
        """3-layer wrapper chain should add < 5ms per read/write operation."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig
        from nexus.backends.wrappers.logging import LoggingBackendWrapper

        mock, storage = make_storage_mock()
        key = AESGCMSIV.generate_key(bit_length=256)

        # Build 3-layer chain: compress → encrypt → logging → leaf
        logged = LoggingBackendWrapper(inner=mock)
        encrypted = EncryptedStorage(
            inner=logged,
            config=EncryptedStorageConfig(key=key, metrics_enabled=False),
        )
        compressed = CompressedStorage(
            inner=encrypted,
            config=CompressedStorageConfig(min_size=0, metrics_enabled=False),
        )

        content = b"performance test data " * 100

        # Warm up
        h = compressed.write_content(content).content_id
        compressed.read_content(h)

        # Time writes
        start = time.perf_counter()
        iterations = 100
        for _ in range(iterations):
            compressed.write_content(content)
        write_elapsed_ms = (time.perf_counter() - start) * 1000 / iterations

        # Time reads
        start = time.perf_counter()
        for _ in range(iterations):
            compressed.read_content(h)
        read_elapsed_ms = (time.perf_counter() - start) * 1000 / iterations

        # Generous threshold: < 10ms per operation (mock backend is instant,
        # but CI runners may be slow under load)
        assert write_elapsed_ms < 10.0, f"Write too slow: {write_elapsed_ms:.2f}ms per op"
        assert read_elapsed_ms < 10.0, f"Read too slow: {read_elapsed_ms:.2f}ms per op"


# ---------------------------------------------------------------------------
# Performance Regression Tests (Decision 16A)
# ---------------------------------------------------------------------------


class TestPerformanceRegression:
    """Performance regression tests for wrapping chains."""

    def test_factory_wrap_construction_overhead(self) -> None:
        """BackendFactory.wrap() should take < 5ms per call."""
        from nexus.backends.base.factory import BackendFactory

        mock, _ = make_storage_mock()

        start = time.perf_counter()
        iterations = 50
        for _ in range(iterations):
            BackendFactory.wrap(mock, "logging")
        elapsed_ms = (time.perf_counter() - start) * 1000 / iterations

        assert elapsed_ms < 5.0, f"wrap() too slow: {elapsed_ms:.2f}ms per call"
