"""EncryptedStorage — transparent encryption decorator for any Backend (#1705).

Wraps an inner Backend and encrypts all content before writing, decrypts
on read. Uses AES-256-GCM-SIV (RFC 8452) for deterministic authenticated
encryption, preserving CAS deduplication (same plaintext + same key =
same ciphertext = same hash).

Follows LEGO Architecture PART 16 (Recursive Wrapping, Mechanism 2).
All non-content Backend operations pass through transparently.

Recommended chain ordering (compress BEFORE encrypt for at-rest storage):

    storage = CompressedStorage(
        inner=EncryptedStorage(
            inner=S3Storage(bucket="data"),
            config=EncryptedStorageConfig(key=key),
        ),
        config=CompressedStorageConfig(),
    )
    # storage.describe() → "compress(zstd) → encrypt(AES-256-GCM-SIV) → s3"

Memory overhead: ~2x content size per operation (plaintext + ciphertext).
CAS blobs are content-addressed chunks, not multi-GB monoliths, so this
is acceptable for typical workloads.

Usage:
    from nexus.backends.wrappers.encrypted import EncryptedStorage, EncryptedStorageConfig

    config = EncryptedStorageConfig(key=encryption_key)
    wrapper = EncryptedStorage(inner=local_backend, config=config)
    # Use wrapper exactly like any Backend

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

from nexus.backends.storage.delegating import DelegatingBackend
from nexus.backends.wrappers.headers import ENCRYPTED_HEADER as _ENCRYPTED_HEADER
from nexus.backends.wrappers.metrics import WrapperMetrics

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend

logger = logging.getLogger(__name__)

_HEADER_LEN = len(_ENCRYPTED_HEADER)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncryptedStorageConfig:
    """Immutable configuration for EncryptedStorage.

    Attributes:
        key: 32-byte AES-256 key. Use AESGCMSIV.generate_key(bit_length=256).
        passthrough_unencrypted: When True, content without the NEXE header
            is returned as-is (for migration from unencrypted storage).
            When False (default), non-NEXE content causes a read error.
        metrics_enabled: Enable OTel encrypt/decrypt/error counters.
    """

    key: bytes
    passthrough_unencrypted: bool = False
    metrics_enabled: bool = True

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise ValueError(
                f"EncryptedStorageConfig key must be 32 bytes (AES-256), got {len(self.key)} bytes"
            )


# ---------------------------------------------------------------------------
# EncryptedStorage
# ---------------------------------------------------------------------------


class EncryptedStorage(DelegatingBackend):
    """Transparent encryption decorator for any Backend implementation.

    Uses AES-256-GCM-SIV (deterministic AEAD) to preserve CAS dedup:
    same plaintext + same key → identical ciphertext → same content hash.

    Encrypted content format:
        NEXE\\x01 (5 bytes header) || ciphertext+tag (no nonce — fixed zero)

    Inherits property delegation, ``__getattr__``, and hook-based
    ``read_content`` / ``write_content`` / ``batch_read_content`` from
    DelegatingBackend. Overrides ``_transform_on_write`` and
    ``_transform_on_read`` to add encryption/decryption.

    CAS dedup guarantee: same key + same plaintext → identical ciphertext
    → same hash. Key rotation breaks dedup for previously-written content.

    Encryption/decryption errors raise exceptions (DelegatingBackend
    catches and returns HandlerResponse.error).
    """

    # Fixed zero nonce for GCM-SIV deterministic encryption.
    # GCM-SIV (RFC 8452) derives the real IV internally from key + plaintext,
    # making nonce reuse safe. A fixed zero nonce ensures same plaintext +
    # same key → identical ciphertext, preserving CAS dedup.
    _ZERO_NONCE = b"\x00" * 12

    def __init__(self, inner: "Backend", config: EncryptedStorageConfig) -> None:
        super().__init__(inner)
        self._config = config
        self._cipher = AESGCMSIV(config.key)
        self._metrics = WrapperMetrics(
            meter_name="nexus.encrypted_storage",
            counter_names=["encrypt_ops", "decrypt_ops", "errors", "passthrough_reads"],
            enabled=config.metrics_enabled,
        )
        logger.info(
            "EncryptedStorage initialized (AES-256-GCM-SIV, key_size=%d, passthrough=%s)",
            len(config.key) * 8,
            config.passthrough_unencrypted,
        )

    # === Chain Introspection ===

    def describe(self) -> str:
        """Return chain description: ``"encrypt(AES-256-GCM-SIV) → {inner}"``."""
        return f"encrypt(AES-256-GCM-SIV) → {self._inner.describe()}"

    @property
    def name(self) -> str:
        return f"encrypted({self._inner.name})"

    # === Transform Hooks ===

    def _transform_on_write(self, content: bytes) -> bytes:
        """Encrypt plaintext with magic header.

        AES-256-GCM-SIV is nonce-misuse resistant (RFC 8452). The actual
        IV is derived deterministically from key + nonce + plaintext, so
        a fixed zero nonce produces deterministic encryption for CAS dedup.

        Format: NEXE\\x01 || ciphertext+tag (no nonce stored — always zero)

        Raises on encryption failure (hard error — DelegatingBackend catches
        and returns error response).
        """
        ct = self._cipher.encrypt(self._ZERO_NONCE, content, None)
        self._metrics.increment("encrypt_ops")
        return _ENCRYPTED_HEADER + ct

    def _transform_on_read(self, data: bytes) -> bytes:
        """Decrypt data with magic header validation.

        Returns:
            Decrypted plaintext.

        Raises:
            ValueError: If data doesn't have the NEXE header (and passthrough
                is disabled) or if decryption fails.
        """
        if not data.startswith(_ENCRYPTED_HEADER):
            if self._config.passthrough_unencrypted:
                self._metrics.increment("passthrough_reads")
                return data
            raise ValueError(
                "Content is not encrypted (missing NEXE header). "
                "Enable passthrough_unencrypted for migration."
            )

        ct = data[_HEADER_LEN:]
        try:
            result = bytes(self._cipher.decrypt(self._ZERO_NONCE, ct, None))
            self._metrics.increment("decrypt_ops")
            return result
        except Exception as e:
            self._metrics.increment("errors")
            raise ValueError(f"Decryption failed: {e}") from e

    # === Stats ===

    def get_encryption_stats(self) -> dict[str, int]:
        """Return encryption/decryption counters."""
        return self._metrics.get_stats()
