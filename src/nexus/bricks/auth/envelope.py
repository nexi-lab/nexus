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

import hashlib
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# DEKCache — TTL + LRU cache for unwrapped DEKs
# ---------------------------------------------------------------------------


def _monotonic() -> float:
    """Indirection so tests can monkeypatch the clock."""
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class DEKCacheKey:
    tenant_id: str
    kek_version: int
    wrapped_dek_sha256: str

    def __repr__(self) -> str:
        return (
            f"DEKCacheKey(tenant={self.tenant_id}, v={self.kek_version}, "
            f"sha256={self.wrapped_dek_sha256})"
        )


class DEKCache:
    """TTL + LRU cache for unwrapped DEKs.

    Keyed by ``(tenant_id, kek_version, sha256(wrapped_dek))`` — the hash, not
    the wrapped bytes, so cache-key logging is safe. Does not cache negative
    results: a KMS/Vault blip shouldn't pin decrypt-failed for the TTL window.

    Thread-safe: callers may share one instance across a thread pool. The
    internal lock is held for the duration of each ``get`` / ``put`` call;
    since both operations are O(1) against an in-memory OrderedDict, contention
    is negligible.
    """

    def __init__(self, *, ttl_seconds: int = 300, max_entries: int = 1024) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: OrderedDict[DEKCacheKey, tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(
        *, tenant_id: str | uuid.UUID, kek_version: int, wrapped_dek: bytes
    ) -> DEKCacheKey:
        return DEKCacheKey(
            tenant_id=str(tenant_id),
            kek_version=kek_version,
            wrapped_dek_sha256=hashlib.sha256(wrapped_dek).hexdigest(),
        )

    def get(self, key: DEKCacheKey) -> bytes | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, dek = entry
            if _monotonic() >= expires_at:
                self._store.pop(key, None)
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return dek

    def put(self, key: DEKCacheKey, dek: bytes) -> None:
        with self._lock:
            self._store[key] = (_monotonic() + self._ttl, dek)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)
