"""AwsKmsProvider — wraps DEKs via AWS KMS.

Uses ``EncryptionContext`` to bind the wrap to ``(tenant_id, aad_fingerprint)``
— any cross-row DEK reuse or tampered AAD fails at KMS rather than only
at the AES-GCM layer locally. Optional dependency: ``boto3``.

AWS KMS automatically rotates the key material behind a CMK annually when
``EnableKeyRotation`` is set. Our ``kek_version`` then tracks provider-config
changes (e.g. swapping the CMK alias) rather than AWS key-material rotation.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING

from nexus.bricks.auth.envelope import (
    EncryptionProvider,
    EnvelopeConfigurationError,
    WrappedDEKInvalid,
)

if TYPE_CHECKING:
    import botocore.client


class AwsKmsProvider(EncryptionProvider):
    def __init__(
        self,
        kms_client: "botocore.client.BaseClient",
        key_id: str,
        *,
        config_version: int = 1,
    ) -> None:
        self._kms = kms_client
        self._key_id = key_id
        self._config_version = config_version
        self._validate_key()

    def _validate_key(self) -> None:
        try:
            self._kms.describe_key(KeyId=self._key_id)
        except Exception as exc:
            raise EnvelopeConfigurationError(
                f"AWS KMS key {self._key_id!r} not accessible: "
                f"{type(exc).__name__}. Grant kms:Encrypt, kms:Decrypt, kms:DescribeKey."
            ) from exc

    @staticmethod
    def _context(tenant_id: uuid.UUID, aad: bytes, kek_version: int) -> dict[str, str]:
        # ``kek_version`` is bound into the EncryptionContext so claiming a
        # different version at unwrap time fails at KMS. Defends against a
        # DB-column tamper where only ``kek_version`` is forged.
        return {
            "tenant_id": str(tenant_id),
            "aad_fingerprint": hashlib.sha256(aad).hexdigest(),
            "kek_version": str(kek_version),
        }

    def current_version(self, *, tenant_id: uuid.UUID) -> int:  # noqa: ARG002
        return self._config_version

    def wrap_dek(self, dek: bytes, *, tenant_id: uuid.UUID, aad: bytes) -> tuple[bytes, int]:
        version = self._config_version
        resp = self._kms.encrypt(
            KeyId=self._key_id,
            Plaintext=dek,
            EncryptionContext=self._context(tenant_id, aad, version),
        )
        return resp["CiphertextBlob"], version

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
        kek_version: int,
    ) -> bytes:
        try:
            resp = self._kms.decrypt(
                CiphertextBlob=wrapped,
                EncryptionContext=self._context(tenant_id, aad, kek_version),
            )
        except Exception as exc:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause=f"KMS decrypt rejected: {type(exc).__name__}",
            ) from exc
        plaintext: bytes = resp["Plaintext"]
        return plaintext


__all__ = ["AwsKmsProvider"]
