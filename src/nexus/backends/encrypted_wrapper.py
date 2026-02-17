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
    from nexus.backends.encrypted_wrapper import EncryptedStorage, EncryptedStorageConfig

    config = EncryptedStorageConfig(key=encryption_key)
    wrapper = EncryptedStorage(inner=local_backend, config=config)
    # Use wrapper exactly like any Backend

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping (Mechanism 2)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

from nexus.backends.delegating import DelegatingBackend
from nexus.backends.wrapper_metrics import WrapperMetrics
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)

# Magic header to identify encrypted content (for passthrough detection).
# "NEXE" + version byte (1).
_ENCRYPTED_HEADER = b"NEXE\x01"
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

    Inherits property delegation and ``__getattr__`` from DelegatingBackend.
    Overrides content read/write operations to add encryption/decryption.

    CAS dedup guarantee: same key + same plaintext → identical ciphertext
    → same hash. Key rotation breaks dedup for previously-written content.

    Decryption errors return HandlerResponse.error() (fail loudly).
    """

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

    # === Encrypted Content Operations ===

    def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> HandlerResponse[str]:
        """Encrypt content and write to inner backend.

        AES-256-GCM-SIV is deterministic: same key + same plaintext =
        same ciphertext, preserving CAS deduplication.
        """
        try:
            ciphertext = self._encrypt(content)
            self._metrics.increment("encrypt_ops")
        except Exception as e:
            self._metrics.increment("errors")
            logger.error("Encryption failed: %s", e)
            return HandlerResponse.error(message=f"Encryption failed: {e}")

        return self._inner.write_content(ciphertext, context=context)

    def read_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> HandlerResponse[bytes]:
        """Read from inner backend and decrypt content."""
        response = self._inner.read_content(content_hash, context=context)
        if not response.success or response.data is None:
            return response

        return self._decrypt_response(response.data)

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: dict[str, "OperationContext"] | None = None,
    ) -> dict[str, bytes | None]:
        """Read batch from inner backend and decrypt each item."""
        raw_results = self._inner.batch_read_content(
            content_hashes, context=context, contexts=contexts
        )

        decrypted: dict[str, bytes | None] = {}
        for content_hash, data in raw_results.items():
            if data is None:
                decrypted[content_hash] = None
                continue

            resp = self._decrypt_response(data)
            decrypted[content_hash] = resp.data if resp.success else None

        return decrypted

    # === Encryption / Decryption Internals ===

    # Fixed zero nonce for GCM-SIV deterministic encryption.
    # GCM-SIV (RFC 8452) derives the real IV internally from key + plaintext,
    # making nonce reuse safe. A fixed zero nonce ensures same plaintext +
    # same key → identical ciphertext, preserving CAS dedup.
    _ZERO_NONCE = b"\x00" * 12

    def _encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext with magic header.

        AES-256-GCM-SIV is nonce-misuse resistant (RFC 8452). The actual
        IV is derived deterministically from key + nonce + plaintext, so
        a fixed zero nonce produces deterministic encryption for CAS dedup.

        Format: NEXE\\x01 || ciphertext+tag (no nonce stored — always zero)
        """
        ct = self._cipher.encrypt(self._ZERO_NONCE, plaintext, None)
        return _ENCRYPTED_HEADER + ct

    def _decrypt(self, data: bytes) -> bytes:
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
        return bytes(self._cipher.decrypt(self._ZERO_NONCE, ct, None))

    def _decrypt_response(self, data: bytes) -> HandlerResponse[bytes]:
        """Decrypt and return as HandlerResponse."""
        try:
            plaintext = self._decrypt(data)
            self._metrics.increment("decrypt_ops")
            return HandlerResponse.ok(data=plaintext, backend_name=self.name)
        except Exception as e:
            self._metrics.increment("errors")
            logger.warning("Decryption failed: %s", e)
            return HandlerResponse.error(message=f"Decryption failed: {e}")

    # === Stats ===

    def get_encryption_stats(self) -> dict[str, int]:
        """Return encryption/decryption counters."""
        return self._metrics.get_stats()
