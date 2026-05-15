"""In-process fake EncryptionProvider for tests + development.

Uses real AES-256-GCM so the wrapped bytes are actual ciphertext (not
identity). Holds a dict of KEKs keyed by version. ``rotate()`` bumps the
current version and mints a new KEK. Exposes ``wrap_count`` /
``unwrap_count`` so contract tests can assert cache amortization.

Never use in production: keys live in process memory and die with it.
"""

from __future__ import annotations

import secrets
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nexus.bricks.auth.envelope import (
    EncryptionProvider,
    WrappedDEKInvalid,
)


class InMemoryEncryptionProvider(EncryptionProvider):
    """Fake provider with real AEAD under the hood.

    Tenant scoping is modelled by mixing ``tenant_id`` into the AAD that wraps
    the DEK — unwrap with the wrong ``tenant_id`` fails the AES-GCM tag, so
    cross-tenant DEK reuse is rejected just like the real providers reject
    mismatched encryption-context / derivation-context.
    """

    def __init__(self) -> None:
        self._versions: dict[int, bytes] = {1: secrets.token_bytes(32)}
        self._current_version = 1
        self._nonce_len = 12
        self.wrap_count = 0
        self.unwrap_count = 0

    def rotate(self) -> None:
        """Bump current_version and mint a new KEK (keep old versions readable)."""
        self._current_version += 1
        self._versions[self._current_version] = secrets.token_bytes(32)

    def current_version(self, *, tenant_id: uuid.UUID) -> int:  # noqa: ARG002
        return self._current_version

    def _context_aad(self, tenant_id: uuid.UUID, kek_version: int, aad: bytes) -> bytes:
        return f"v=inmem|tenant={tenant_id}|kek={kek_version}|".encode() + aad

    def wrap_dek(self, dek: bytes, *, tenant_id: uuid.UUID, aad: bytes) -> tuple[bytes, int]:
        self.wrap_count += 1
        version = self._current_version
        kek = self._versions[version]
        nonce = secrets.token_bytes(self._nonce_len)
        ct = AESGCM(kek).encrypt(nonce, dek, self._context_aad(tenant_id, version, aad))
        return nonce + ct, version

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
        kek_version: int,
    ) -> bytes:
        self.unwrap_count += 1
        if kek_version not in self._versions:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause="kek_version not known to InMemoryEncryptionProvider",
            )
        kek = self._versions[kek_version]
        nonce, ct = wrapped[: self._nonce_len], wrapped[self._nonce_len :]
        try:
            return AESGCM(kek).decrypt(nonce, ct, self._context_aad(tenant_id, kek_version, aad))
        except InvalidTag as exc:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause="AES-GCM tag mismatch (wrong tenant_id/aad/kek_version)",
            ) from exc


__all__ = ["InMemoryEncryptionProvider"]
