"""VaultTransitProvider — wraps DEKs via Vault's transit secrets engine.

Requires a derived-context key (``derived=true``) so the per-tenant
``context`` param produces a per-tenant subkey without creating one key per
tenant. See:
  https://developer.hashicorp.com/vault/docs/secrets/transit

Optional dependency: ``hvac``. Install via the ``vault`` extra.
"""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

from nexus.bricks.auth.envelope import (
    EncryptionProvider,
    EnvelopeConfigurationError,
    WrappedDEKInvalid,
)

if TYPE_CHECKING:
    import hvac


class VaultTransitProvider(EncryptionProvider):
    def __init__(
        self,
        vault_client: "hvac.Client",
        key_name: str,
        *,
        mount_point: str = "transit",
    ) -> None:
        try:
            import hvac  # noqa: F401
        except ImportError as exc:  # pragma: no cover — optional dep
            raise EnvelopeConfigurationError(
                "VaultTransitProvider requires the `hvac` package. "
                "Install with: pip install 'nexus[vault]'"
            ) from exc
        self._client = vault_client
        self._key_name = key_name
        self._mount = mount_point
        self._validate_key()

    def _validate_key(self) -> None:
        try:
            resp = self._client.secrets.transit.read_key(
                name=self._key_name, mount_point=self._mount
            )
        except Exception as exc:
            raise EnvelopeConfigurationError(
                f"Vault transit key {self._key_name!r} not readable "
                f"on mount {self._mount!r}: {type(exc).__name__}"
            ) from exc
        data = resp.get("data", {})
        if not data.get("derived"):
            raise EnvelopeConfigurationError(
                f"Vault transit key {self._key_name!r} must have derived=true. "
                f"Run: vault write -f {self._mount}/keys/{self._key_name} derived=true"
            )

    def current_version(self, *, tenant_id: uuid.UUID) -> int:  # noqa: ARG002
        resp = self._client.secrets.transit.read_key(name=self._key_name, mount_point=self._mount)
        data = resp.get("data", {})
        return int(data.get("latest_version", 1))

    def _context_b64(self, tenant_id: uuid.UUID) -> str:
        return base64.b64encode(str(tenant_id).encode("utf-8")).decode("ascii")

    def wrap_dek(
        self,
        dek: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,  # noqa: ARG002 — bound at the AESGCM layer; Vault Transit has no separate AAD param
    ) -> tuple[bytes, int]:
        resp = self._client.secrets.transit.encrypt_data(
            name=self._key_name,
            plaintext=base64.b64encode(dek).decode("ascii"),
            context=self._context_b64(tenant_id),
            mount_point=self._mount,
        )
        ciphertext = resp["data"]["ciphertext"]  # "vault:v<N>:<base64>"
        version = int(ciphertext.split(":")[1].lstrip("v"))
        return ciphertext.encode("utf-8"), version

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,  # noqa: ARG002
        kek_version: int,
    ) -> bytes:
        try:
            resp = self._client.secrets.transit.decrypt_data(
                name=self._key_name,
                ciphertext=wrapped.decode("utf-8"),
                context=self._context_b64(tenant_id),
                mount_point=self._mount,
            )
        except Exception as exc:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause=f"Vault transit decrypt rejected: {type(exc).__name__}",
            ) from exc
        return base64.b64decode(resp["data"]["plaintext"])


__all__ = ["VaultTransitProvider"]
