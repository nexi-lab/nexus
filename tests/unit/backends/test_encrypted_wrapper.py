"""Unit tests for EncryptedStorage wrapper (#1705).

TDD: Tests written BEFORE implementation to drive the interface design.

Tests verify:
1. describe() chain output
2. Encrypt → decrypt roundtrip correctness
3. CAS dedup preservation (GCM-SIV determinism)
4. Decrypt error handling (wrong key, corruption)
5. passthrough_unencrypted migration mode
6. Empty content edge case
7. Delegation of non-content ops
8. Config validation
9. batch_read_content per-item decryption

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
"""


from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.backends.backend import Backend
from nexus.core.protocols.describable import Describable
from nexus.core.response import HandlerResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_leaf(name: str = "local") -> MagicMock:
    """Create a mock leaf backend that stores content in-memory."""
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


def _generate_key() -> bytes:
    """Generate a valid 32-byte AES-256 key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

    return AESGCMSIV.generate_key(bit_length=256)


def _make_storage_mock() -> tuple[MagicMock, dict[str, bytes]]:
    """Create a mock leaf that actually stores/retrieves encrypted content.

    Returns (mock, storage_dict) where storage_dict maps hash→bytes.
    """
    storage: dict[str, bytes] = {}
    mock = _make_leaf("storage-mock")

    def write_content(content: bytes, context: object = None) -> HandlerResponse:
        import hashlib

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


class TestEncryptedDescribe:
    """describe() should prepend encryption layer info."""

    def test_single_wrapper(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        leaf = _make_leaf("local")
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "encrypt(AES-256-GCM-SIV) → local"

    def test_chain_with_logging(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        leaf = _make_leaf("s3")
        leaf.describe.return_value = "logging → s3"
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "encrypt(AES-256-GCM-SIV) → logging → s3"

    def test_is_describable(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        leaf = _make_leaf("local")
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert isinstance(wrapper, Describable)


# ---------------------------------------------------------------------------
# Roundtrip Tests
# ---------------------------------------------------------------------------


class TestEncryptedRoundtrip:
    """Write + read should return identical plaintext."""

    def test_basic_roundtrip(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = b"hello world"
        write_resp = wrapper.write_content(plaintext)
        assert write_resp.success
        content_hash = write_resp.data

        read_resp = wrapper.read_content(content_hash)
        assert read_resp.success
        assert read_resp.data == plaintext

    def test_binary_roundtrip(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = bytes(range(256))  # All byte values
        write_resp = wrapper.write_content(plaintext)
        assert write_resp.success

        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == plaintext

    def test_large_content_roundtrip(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = b"A" * (1024 * 1024)  # 1MB
        write_resp = wrapper.write_content(plaintext)
        assert write_resp.success

        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == plaintext


# ---------------------------------------------------------------------------
# CAS Dedup Tests (GCM-SIV determinism)
# ---------------------------------------------------------------------------


class TestEncryptedCASDedup:
    """Same plaintext + same key should produce same ciphertext → same hash."""

    def test_deterministic_encryption(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        key = _generate_key()
        config = EncryptedStorageConfig(key=key, metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = b"deterministic content"
        hash1 = wrapper.write_content(plaintext).data
        hash2 = wrapper.write_content(plaintext).data
        assert hash1 == hash2, "GCM-SIV should produce identical ciphertext for identical plaintext"

    def test_different_plaintext_different_hash(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        hash1 = wrapper.write_content(b"content A").data
        hash2 = wrapper.write_content(b"content B").data
        assert hash1 != hash2


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestEncryptedErrors:
    """Decryption errors should fail loudly."""

    def test_corrupted_ciphertext(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        # Write valid content
        write_resp = wrapper.write_content(b"valid data")
        content_hash = write_resp.data

        # Corrupt the stored ciphertext
        stored = storage[content_hash]
        storage[content_hash] = stored[:10] + b"\xff" * 10 + stored[20:]

        # Read should fail with error response, not raise
        read_resp = wrapper.read_content(content_hash)
        assert not read_resp.success
        assert read_resp.error_message is not None
        assert (
            "decrypt" in read_resp.error_message.lower()
            or "error" in read_resp.error_message.lower()
        )

    def test_wrong_key_fails(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        key1 = _generate_key()
        key2 = _generate_key()

        wrapper1 = EncryptedStorage(
            inner=mock, config=EncryptedStorageConfig(key=key1, metrics_enabled=False)
        )
        wrapper2 = EncryptedStorage(
            inner=mock, config=EncryptedStorageConfig(key=key2, metrics_enabled=False)
        )

        # Write with key1
        write_resp = wrapper1.write_content(b"secret data")
        content_hash = write_resp.data

        # Read with key2 should fail
        read_resp = wrapper2.read_content(content_hash)
        assert not read_resp.success
        assert read_resp.error_message is not None

    def test_inner_read_error_propagated(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        leaf = _make_leaf("local")
        leaf.read_content.return_value = HandlerResponse.error(message="disk I/O error")
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)

        read_resp = wrapper.read_content("some-hash")
        assert not read_resp.success
        assert read_resp.error_message is not None
        assert "disk I/O error" in read_resp.error_message


# ---------------------------------------------------------------------------
# Passthrough (unencrypted migration) Tests
# ---------------------------------------------------------------------------


class TestEncryptedPassthrough:
    """passthrough_unencrypted=True should handle pre-encryption content."""

    def test_passthrough_reads_unencrypted_content(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(
            key=_generate_key(), passthrough_unencrypted=True, metrics_enabled=False
        )
        wrapper = EncryptedStorage(inner=mock, config=config)

        # Store unencrypted content directly in mock
        import hashlib

        raw = b"pre-encryption content"
        h = hashlib.sha256(raw).hexdigest()
        storage[h] = raw

        # With passthrough, should return raw content
        read_resp = wrapper.read_content(h)
        assert read_resp.success
        assert read_resp.data == raw

    def test_no_passthrough_rejects_unencrypted_content(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(
            key=_generate_key(), passthrough_unencrypted=False, metrics_enabled=False
        )
        wrapper = EncryptedStorage(inner=mock, config=config)

        # Store unencrypted content directly
        import hashlib

        raw = b"pre-encryption content"
        h = hashlib.sha256(raw).hexdigest()
        storage[h] = raw

        # Without passthrough, should fail
        read_resp = wrapper.read_content(h)
        assert not read_resp.success


# ---------------------------------------------------------------------------
# Empty Content Edge Case
# ---------------------------------------------------------------------------


class TestEncryptedEmptyContent:
    """Empty content should encrypt/decrypt correctly."""

    def test_empty_roundtrip(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        write_resp = wrapper.write_content(b"")
        assert write_resp.success

        read_resp = wrapper.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == b""


# ---------------------------------------------------------------------------
# Delegation Tests
# ---------------------------------------------------------------------------


class TestEncryptedDelegation:
    """Non-content ops should pass through to inner backend."""

    def test_mkdir_delegates(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        leaf = _make_leaf("local")
        leaf.mkdir.return_value = HandlerResponse.ok(data=None)
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)

        result = wrapper.mkdir("/test", parents=True)
        leaf.mkdir.assert_called_once()
        assert result.success

    def test_rmdir_delegates(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        leaf = _make_leaf("local")
        leaf.rmdir.return_value = HandlerResponse.ok(data=None)
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)

        result = wrapper.rmdir("/test", recursive=True)
        leaf.rmdir.assert_called_once()
        assert result.success


# ---------------------------------------------------------------------------
# Config Validation Tests
# ---------------------------------------------------------------------------


class TestEncryptedConfig:
    """Config validation at construction time."""

    def test_invalid_key_length_raises(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorageConfig

        with pytest.raises(ValueError, match="key.*32 bytes"):
            EncryptedStorageConfig(key=b"too-short")

    def test_valid_key_accepted(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorageConfig

        config = EncryptedStorageConfig(key=_generate_key())
        assert len(config.key) == 32


# ---------------------------------------------------------------------------
# Batch Operation Tests
# ---------------------------------------------------------------------------


class TestEncryptedBatch:
    """batch_read_content should decrypt each item individually."""

    def test_batch_read_decrypts_all(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        # Write 3 items
        h1 = wrapper.write_content(b"alpha").data
        h2 = wrapper.write_content(b"beta").data
        h3 = wrapper.write_content(b"gamma").data

        # Batch read
        results = wrapper.batch_read_content([h1, h2, h3])
        assert results[h1] == b"alpha"
        assert results[h2] == b"beta"
        assert results[h3] == b"gamma"

    def test_batch_read_handles_missing(self) -> None:
        from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

        mock, storage = _make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        h1 = wrapper.write_content(b"exists").data
        results = wrapper.batch_read_content([h1, "nonexistent"])
        assert results[h1] == b"exists"
        assert results["nonexistent"] is None
