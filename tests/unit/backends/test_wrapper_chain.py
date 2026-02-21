"""Integration tests for multi-wrapper chain data flow (#1705).

Tests verify that the full composition chain (compress → encrypt → leaf)
correctly transforms data on write and reverses transforms on read.

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
"""

import hashlib
from unittest.mock import MagicMock, PropertyMock

from nexus.backends.backend import Backend
from nexus.core.response import HandlerResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_leaf(name: str = "local") -> MagicMock:
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
    """Create a mock leaf with real storage semantics."""
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
    return mock, storage


# ---------------------------------------------------------------------------
# Chain Composition Tests
# ---------------------------------------------------------------------------


class TestCompressEncryptChain:
    """Full chain: compress → encrypt → leaf (correct ordering)."""

    def test_describe_full_chain(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, _ = _make_storage_mock()
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

        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
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
        assert write_resp.success

        # Verify stored content is neither plaintext nor just compressed
        stored = storage[write_resp.data]
        assert stored != plaintext, "Stored content should be encrypted"

        # Read back through chain
        read_resp = compressed.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == plaintext

    def test_chain_dedup(self) -> None:
        """Same content through same chain should produce same hash."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
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
        h1 = compressed.write_content(content).data
        h2 = compressed.write_content(content).data
        assert h1 == h2

    def test_chain_batch_read(self) -> None:
        """batch_read_content through chain should decompress + decrypt all."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
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
        hashes = [compressed.write_content(item).data for item in items]

        results = compressed.batch_read_content(hashes)
        for h, expected in zip(hashes, items):
            assert results[h] == expected


class TestEncryptCompressChain:
    """Reversed chain: encrypt → compress → leaf (suboptimal but valid)."""

    def test_describe_reversed(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, _ = _make_storage_mock()
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

        from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
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
        assert write_resp.success

        read_resp = encrypted.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == plaintext
