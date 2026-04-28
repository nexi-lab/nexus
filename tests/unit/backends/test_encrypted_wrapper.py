"""Unit tests for EncryptedStorage wrapper (#1705, #2077).

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
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

import pytest

from nexus.contracts.describable import Describable
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.object_store import WriteResult
from tests.unit.backends.wrapper_test_helpers import make_leaf, make_storage_mock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_key() -> bytes:
    """Generate a valid 32-byte AES-256 key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

    return AESGCMSIV.generate_key(bit_length=256)


# ---------------------------------------------------------------------------
# describe() Tests
# ---------------------------------------------------------------------------


class TestEncryptedDescribe:
    """describe() should prepend encryption layer info."""

    def test_single_wrapper(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        leaf = make_leaf("local")
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "encrypt(AES-256-GCM-SIV) → local"

    def test_chain_with_logging(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        leaf = make_leaf("s3")
        leaf.describe.return_value = "logging → s3"
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert wrapper.describe() == "encrypt(AES-256-GCM-SIV) → logging → s3"

    def test_is_describable(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        leaf = make_leaf("local")
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)
        assert isinstance(wrapper, Describable)


# ---------------------------------------------------------------------------
# Roundtrip Tests
# ---------------------------------------------------------------------------


class TestEncryptedRoundtrip:
    """Write + read should return identical plaintext."""

    def test_basic_roundtrip(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = b"hello world"
        write_resp = wrapper.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)
        content_id = write_resp.content_id

        read_resp = wrapper.read_content(content_id)
        assert read_resp == plaintext

    def test_binary_roundtrip(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = bytes(range(256))  # All byte values
        write_resp = wrapper.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)

        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == plaintext

    def test_large_content_roundtrip(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = b"A" * (1024 * 1024)  # 1MB
        write_resp = wrapper.write_content(plaintext)
        assert isinstance(write_resp, WriteResult)

        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == plaintext


# ---------------------------------------------------------------------------
# CAS Dedup Tests (GCM-SIV determinism)
# ---------------------------------------------------------------------------


class TestEncryptedCASDedup:
    """Same plaintext + same key should produce same ciphertext → same hash."""

    def test_deterministic_encryption(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        key = _generate_key()
        config = EncryptedStorageConfig(key=key, metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        plaintext = b"deterministic content"
        hash1 = wrapper.write_content(plaintext).content_id
        hash2 = wrapper.write_content(plaintext).content_id
        assert hash1 == hash2, "GCM-SIV should produce identical ciphertext for identical plaintext"

    def test_different_plaintext_different_hash(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        hash1 = wrapper.write_content(b"content A").content_id
        hash2 = wrapper.write_content(b"content B").content_id
        assert hash1 != hash2


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestEncryptedErrors:
    """Decryption errors should fail loudly."""

    def test_corrupted_ciphertext(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        # Write valid content
        write_resp = wrapper.write_content(b"valid data")
        content_id = write_resp.content_id

        # Corrupt the stored ciphertext
        stored = storage[content_id]
        storage[content_id] = stored[:10] + b"\xff" * 10 + stored[20:]

        # Read should raise ValueError from failed decryption
        with pytest.raises(ValueError):
            wrapper.read_content(content_id)

    def test_wrong_key_fails(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
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
        content_id = write_resp.content_id

        # Read with key2 should raise ValueError
        with pytest.raises(ValueError):
            wrapper2.read_content(content_id)

    def test_inner_read_error_propagated(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        leaf = make_leaf("local")
        leaf.read_content.side_effect = NexusFileNotFoundError("disk I/O error")
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)

        with pytest.raises(NexusFileNotFoundError, match="disk I/O error"):
            wrapper.read_content("some-hash")


# ---------------------------------------------------------------------------
# Passthrough (unencrypted migration) Tests
# ---------------------------------------------------------------------------


class TestEncryptedPassthrough:
    """passthrough_unencrypted=True should handle pre-encryption content."""

    def test_passthrough_reads_unencrypted_content(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
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
        assert read_resp == raw

    def test_no_passthrough_rejects_unencrypted_content(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(
            key=_generate_key(), passthrough_unencrypted=False, metrics_enabled=False
        )
        wrapper = EncryptedStorage(inner=mock, config=config)

        # Store unencrypted content directly
        import hashlib

        raw = b"pre-encryption content"
        h = hashlib.sha256(raw).hexdigest()
        storage[h] = raw

        # Without passthrough, should raise ValueError
        with pytest.raises(ValueError):
            wrapper.read_content(h)


# ---------------------------------------------------------------------------
# Empty Content Edge Case
# ---------------------------------------------------------------------------


class TestEncryptedEmptyContent:
    """Empty content should encrypt/decrypt correctly."""

    def test_empty_roundtrip(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        write_resp = wrapper.write_content(b"")
        assert isinstance(write_resp, WriteResult)

        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == b""


# ---------------------------------------------------------------------------
# Delegation Tests
# ---------------------------------------------------------------------------


class TestEncryptedDelegation:
    """Non-content ops should pass through to inner backend."""

    def test_mkdir_delegates(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        leaf = make_leaf("local")
        leaf.mkdir.return_value = None
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)

        result = wrapper.mkdir("/test", parents=True)
        leaf.mkdir.assert_called_once()
        assert result is None

    def test_rmdir_delegates(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        leaf = make_leaf("local")
        leaf.rmdir.return_value = None
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=leaf, config=config)

        result = wrapper.rmdir("/test", recursive=True)
        leaf.rmdir.assert_called_once()
        assert result is None


# ---------------------------------------------------------------------------
# Config Validation Tests
# ---------------------------------------------------------------------------


class TestEncryptedConfig:
    """Config validation at construction time."""

    def test_invalid_key_length_raises(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorageConfig

        with pytest.raises(ValueError, match="key.*32 bytes"):
            EncryptedStorageConfig(key=b"too-short")

    def test_valid_key_accepted(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorageConfig

        config = EncryptedStorageConfig(key=_generate_key())
        assert len(config.key) == 32


# ---------------------------------------------------------------------------
# Batch Operation Tests
# ---------------------------------------------------------------------------


class TestEncryptedBatch:
    """batch_read_content should decrypt each item individually."""

    def test_batch_read_decrypts_all(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        # Write 3 items
        h1 = wrapper.write_content(b"alpha").content_id
        h2 = wrapper.write_content(b"beta").content_id
        h3 = wrapper.write_content(b"gamma").content_id

        # Batch read
        results = wrapper.batch_read_content([h1, h2, h3])
        assert results[h1] == b"alpha"
        assert results[h2] == b"beta"
        assert results[h3] == b"gamma"

    def test_batch_read_handles_missing(self) -> None:
        from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

        mock, storage = make_storage_mock()
        config = EncryptedStorageConfig(key=_generate_key(), metrics_enabled=False)
        wrapper = EncryptedStorage(inner=mock, config=config)

        h1 = wrapper.write_content(b"exists").content_id
        results = wrapper.batch_read_content([h1, "nonexistent"])
        assert results[h1] == b"exists"
        assert results["nonexistent"] is None
