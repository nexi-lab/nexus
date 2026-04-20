"""RFC 8414 OAuth 2.0 Authorization Server Metadata + OIDC Discovery.

Fetches ``/.well-known/oauth-authorization-server``. If that returns 404 or
non-JSON, falls back to ``/.well-known/openid-configuration`` (OIDC Discovery),
which carries the same endpoint fields for most real-world providers (Auth0,
Okta, Keycloak, Google).

Usage::

    client = DiscoveryClient()
    meta = await client.fetch("https://accounts.google.com")
    provider = UniversalOAuthProvider(
        client_id=...,
        client_secret=...,
        scopes=["openid"],
        provider_name="google-oidc",
        discovery_metadata=meta,
    )
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class DiscoveryError(Exception):
    """Discovery fetch or parse failed."""


@dataclass(frozen=True, slots=True)
class DiscoveryMetadata:
    """Parsed RFC 8414 / OIDC Discovery metadata."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None = None
    introspection_endpoint: str | None = None
    registration_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    jwks_uri: str | None = None
    scopes_supported: tuple[str, ...] = ()
    response_types_supported: tuple[str, ...] = ()
    grant_types_supported: tuple[str, ...] = ()
    code_challenge_methods_supported: tuple[str, ...] = ()
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscoveryMetadata":
        required = ("issuer", "authorization_endpoint", "token_endpoint")
        missing = [k for k in required if not data.get(k)]
        if missing:
            raise DiscoveryError(
                f"Discovery document missing required field(s): {', '.join(missing)}"
            )
        return cls(
            issuer=data["issuer"],
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            revocation_endpoint=data.get("revocation_endpoint"),
            introspection_endpoint=data.get("introspection_endpoint"),
            registration_endpoint=data.get("registration_endpoint"),
            userinfo_endpoint=data.get("userinfo_endpoint"),
            jwks_uri=data.get("jwks_uri"),
            scopes_supported=tuple(data.get("scopes_supported") or ()),
            response_types_supported=tuple(data.get("response_types_supported") or ()),
            grant_types_supported=tuple(data.get("grant_types_supported") or ()),
            code_challenge_methods_supported=tuple(
                data.get("code_challenge_methods_supported") or ()
            ),
            token_endpoint_auth_methods_supported=tuple(
                data.get("token_endpoint_auth_methods_supported") or ()
            ),
            raw=data,
        )


_WELL_KNOWN_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)


class DiscoveryClient:
    """Fetch + parse authorization server metadata."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(self, issuer_url: str) -> DiscoveryMetadata:
        """Fetch metadata for ``issuer_url`` and validate ``issuer`` matches."""
        issuer_url = issuer_url.rstrip("/")
        last_error: Exception | None = None
        for path in _WELL_KNOWN_PATHS:
            url = f"{issuer_url}{path}"
            try:
                data = await self._fetch_one(url)
            except DiscoveryError as exc:
                last_error = exc
                continue
            meta = DiscoveryMetadata.from_dict(data)
            if meta.issuer.rstrip("/") != issuer_url:
                raise DiscoveryError(
                    f"Issuer mismatch: expected {issuer_url}, discovery returned {meta.issuer}"
                )
            return meta
        raise DiscoveryError(
            f"No discovery document at {issuer_url} (tried {list(_WELL_KNOWN_PATHS)})"
        ) from last_error

    async def _fetch_one(self, url: str) -> dict[str, Any]:
        client = self._client
        opened = False
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            opened = True
        try:
            try:
                response = await client.get(url)
            except httpx.TimeoutException as exc:
                raise DiscoveryError(f"Timeout fetching {url}") from exc
            except httpx.RequestError as exc:
                raise DiscoveryError(f"Network error fetching {url}: {exc}") from exc
            if response.status_code == 404:
                raise DiscoveryError(f"{url} returned 404")
            if response.status_code >= 400:
                raise DiscoveryError(
                    f"{url} returned {response.status_code}: {response.text[:200]}"
                )
            try:
                parsed: dict[str, Any] = response.json()
                return parsed
            except _json.JSONDecodeError as exc:
                raise DiscoveryError(f"{url} returned non-JSON body") from exc
        finally:
            if opened:
                await client.aclose()
