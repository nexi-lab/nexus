"""Envelope encryption primitives for PostgresAuthProfileStore (issue #3803).

Provides:
  - AESGCMEnvelope: AES-256-GCM wrapper for per-row ciphertext + DEK.
  - EncryptionProvider: Protocol for KEK-level DEK wrap/unwrap (impls land in
    envelope_providers/).
  - DEKCache: in-process TTL+LRU cache of unwrapped DEKs.
  - EnvelopeError hierarchy with no-plaintext repr discipline.

Design: docs/superpowers/specs/2026-04-18-issue-3803-envelope-encryption-design.md
"""

from __future__ import annotations

import secrets
import uuid
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Error hierarchy — no plaintext, wrapped-DEK, or ciphertext bytes in str/repr
# ---------------------------------------------------------------------------


class EnvelopeError(Exception):
    """Root of every error raised by the envelope subsystem."""


class EnvelopeConfigurationError(EnvelopeError):
    """Provider is misconfigured (e.g. Vault transit key not derived=true)."""


class DecryptionFailed(EnvelopeError):
    """Generic decrypt failure. Concrete subclasses narrow the reason."""


class AADMismatch(DecryptionFailed):
    """Stored AAD column does not match the expected tenant|principal|id."""


class WrappedDEKInvalid(DecryptionFailed):
    """Provider refused to unwrap the DEK (IAM, mismatched context, corrupt)."""


class CiphertextCorrupted(DecryptionFailed):
    """AES-GCM tag verification failed — ciphertext/nonce/AAD tampered."""


# ---------------------------------------------------------------------------
# AES-256-GCM primitive
# ---------------------------------------------------------------------------


class AESGCMEnvelope:
    """Thin wrapper over `cryptography`'s AESGCM with our invariants baked in.

    - DEK is 32 bytes (AES-256).
    - Nonce is 12 bytes, freshly generated per ``encrypt`` call.
    - AAD is bound into the AEAD tag; tamper raises ``CiphertextCorrupted``.
    """

    NONCE_LEN = 12
    DEK_LEN = 32

    def encrypt(self, dek: bytes, plaintext: bytes, *, aad: bytes) -> tuple[bytes, bytes]:
        if len(dek) != self.DEK_LEN:
            raise ValueError(f"DEK must be {self.DEK_LEN} bytes, got {len(dek)}")
        nonce = secrets.token_bytes(self.NONCE_LEN)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        return nonce, ciphertext

    def decrypt(self, dek: bytes, nonce: bytes, ciphertext: bytes, *, aad: bytes) -> bytes:
        if len(dek) != self.DEK_LEN:
            raise ValueError(f"DEK must be {self.DEK_LEN} bytes, got {len(dek)}")
        try:
            return AESGCM(dek).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise CiphertextCorrupted("AES-GCM tag verification failed") from exc


# ---------------------------------------------------------------------------
# EncryptionProvider Protocol (impls in envelope_providers/)
# ---------------------------------------------------------------------------


@runtime_checkable
class EncryptionProvider(Protocol):
    """KEK-level DEK wrap/unwrap.

    ``wrap_dek`` always uses the provider's current version and returns it
    alongside the wrapped bytes. ``unwrap_dek`` takes the stored ``kek_version``
    because rows persist history — Vault Transit takes ``key_version`` on
    decrypt natively; AWS KMS ignores it (the ciphertext blob embeds the key
    version internally) so ``kek_version`` is bookkeeping-only there.
    """

    def current_version(self, *, tenant_id: uuid.UUID) -> int: ...

    def wrap_dek(
        self,
        dek: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
    ) -> tuple[bytes, int]:
        """Return ``(wrapped_bytes, kek_version)``."""
        ...

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
        kek_version: int,
    ) -> bytes: ...
