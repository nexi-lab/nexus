"""Bindings for the native host's authenticated principal and credential store."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from nexus.cli.clients.base import BaseServiceClient, NexusAPIError


class AuthClient(BaseServiceClient):
    def whoami(self, *, timeout: float | None = None) -> dict[str, Any]:
        result = self._request("GET", "/v2/auth/whoami", timeout=timeout)
        if not (
            all(
                isinstance(result.get(name), str)
                for name in ("subject_type", "subject_id", "user_id", "zone_id")
            )
            and type(result.get("is_admin")) is bool
            and isinstance(result.get("zone_perms"), list)
        ):
            raise NexusAPIError(502, "Invalid native identity response")
        return result

    def list_keys(
        self,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        is_admin: bool | None = None,
        include_revoked: bool = False,
    ) -> dict[str, Any]:
        result = self._request(
            "GET",
            "/v2/auth/keys",
            params={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "is_admin": is_admin,
                "include_revoked": include_revoked,
            },
        )
        if not isinstance(result.get("keys"), list):
            raise NexusAPIError(502, "Invalid native key listing")
        return result

    def mint_key(
        self,
        *,
        subject_type: str,
        subject_id: str,
        zones: tuple[str, ...] = (),
        name: str = "",
        admin: bool = False,
        expires_at_ms: int = 0,
        allow_existing: bool = False,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/v2/auth/keys",
            json_body={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "zones": list(zones),
                "name": name,
                "admin": admin,
                "expires_at_ms": expires_at_ms,
                "allow_existing": allow_existing,
            },
        )
        if not (
            all(
                isinstance(result.get(field), str) and result[field]
                for field in ("key", "key_hash", "key_id")
            )
            and isinstance(result.get("record"), dict)
        ):
            raise NexusAPIError(502, "Invalid native key creation receipt")
        return result

    def revoke_key(self, key_hash: str) -> dict[str, Any]:
        result = self._request("DELETE", f"/v2/auth/keys/{quote(key_hash, safe='')}")
        if type(result.get("existed")) is not bool or result.get("key_hash") != key_hash:
            raise NexusAPIError(502, "Invalid native key revocation receipt")
        return result
