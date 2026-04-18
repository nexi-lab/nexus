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
    """Root of every error raised by the envelope subsystem.

    All subclasses expose ``from_row(tenant_id, profile_id, kek_version, cause)``
    so call sites never build error messages inline — that discipline is what
    keeps plaintext / wrapped-DEK bytes out of ``__str__`` / ``__repr__``.
    """

    def __init__(
        self,
        message: str,
        *,
        tenant_id: uuid.UUID | None = None,
        profile_id: str | None = None,
        kek_version: int | None = None,
        cause: str | None = None,
    ) -> None:
        super().__init__(message)
        self._message = message
        self.tenant_id = tenant_id
        self.profile_id = profile_id
        self.kek_version = kek_version
        self.cause = cause

    @classmethod
    def from_row(
        cls,
        *,
        tenant_id: uuid.UUID,
        profile_id: str,
        kek_version: int,
        cause: str,
    ) -> "EnvelopeError":
        return cls(
            f"{cls.__name__} tenant={tenant_id} profile={profile_id} "
            f"kek_version={kek_version} cause={cause}",
            tenant_id=tenant_id,
            profile_id=profile_id,
            kek_version=kek_version,
            cause=cause,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(tenant_id={self.tenant_id!s}, "
            f"profile_id={self.profile_id!r}, kek_version={self.kek_version!r}, "
            f"cause={self.cause!r})"
        )


class EnvelopeConfigurationError(EnvelopeError):
    pass


class DecryptionFailed(EnvelopeError):
    pass


class AADMismatch(DecryptionFailed):
    pass


class WrappedDEKInvalid(DecryptionFailed):
    pass


class CiphertextCorrupted(DecryptionFailed):
    pass


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
            raise CiphertextCorrupted(
                "AES-GCM tag verification failed",
                cause="InvalidTag",
            ) from exc


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
