"""Universal RFC 6749 OAuth provider.

Use when the target provider is RFC 6749 compliant and endpoints are either
published via RFC 8414 discovery or can be supplied explicitly. Vendor quirks
that cannot be captured by the two knobs here (``scope_format``,
``scope_on_refresh``) should live in a dedicated subclass — see
:mod:`nexus.lib.oauth.providers` for examples.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from nexus.lib.oauth.base import BaseOAuthProvider
from nexus.lib.oauth.discovery import DiscoveryMetadata
from nexus.lib.oauth.types import OAuthCredential, OAuthError

_SCOPE_SEPARATORS = {"space": " ", "comma": ",", "plus": "+"}


class UniversalOAuthProvider(BaseOAuthProvider):
    """RFC 6749 provider with configurable endpoints and vendor knobs."""

    REVOKE_ENDPOINT: str = ""
    INTROSPECTION_ENDPOINT: str = ""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        *,
        discovery_metadata: DiscoveryMetadata | None = None,
        authorization_endpoint: str | None = None,
        token_endpoint: str | None = None,
        revocation_endpoint: str | None = None,
        introspection_endpoint: str | None = None,
        scope_format: str = "space",
        scope_on_refresh: bool = False,
        requires_pkce: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # Endpoints: discovery metadata first, explicit kwargs override.
        if discovery_metadata is not None:
            self.AUTHORIZATION_ENDPOINT = discovery_metadata.authorization_endpoint
            self.TOKEN_ENDPOINT = discovery_metadata.token_endpoint
            self.REVOKE_ENDPOINT = discovery_metadata.revocation_endpoint or ""
            self.INTROSPECTION_ENDPOINT = discovery_metadata.introspection_endpoint or ""
        if authorization_endpoint:
            self.AUTHORIZATION_ENDPOINT = authorization_endpoint
        if token_endpoint:
            self.TOKEN_ENDPOINT = token_endpoint
        if revocation_endpoint:
            self.REVOKE_ENDPOINT = revocation_endpoint
        if introspection_endpoint:
            self.INTROSPECTION_ENDPOINT = introspection_endpoint

        if not self.TOKEN_ENDPOINT or not self.AUTHORIZATION_ENDPOINT:
            raise OAuthError(
                "UniversalOAuthProvider requires token_endpoint and "
                "authorization_endpoint (via discovery_metadata or explicit kwargs)."
            )

        if scope_format not in _SCOPE_SEPARATORS:
            raise OAuthError(
                f"Unknown scope_format={scope_format!r}. "
                f"Expected one of: {sorted(_SCOPE_SEPARATORS)}"
            )
        self._scope_sep = _SCOPE_SEPARATORS[scope_format]
        self._scope_on_refresh = scope_on_refresh
        self.requires_pkce = requires_pkce

        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            provider_name=provider_name,
            http_client=http_client,
        )

    def _scope_string(self) -> str:
        return self._scope_sep.join(self.scopes)

    def get_authorization_url(
        self,
        state: str | None = None,
        *,
        redirect_uri: str | None = None,
        extra_params: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> str:
        # Per-call redirect_uri override is resolved into a local variable —
        # never mutate ``self.redirect_uri``, which is shared across concurrent
        # requests (see GoogleOAuthProvider thread-safety fix).
        effective_redirect_uri = redirect_uri or self.redirect_uri
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": effective_redirect_uri,
            "scope": self._scope_string(),
        }
        if state:
            params["state"] = state
        if extra_params:
            params.update(extra_params)
        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]:
        redirect_uri = kwargs.get("redirect_uri") or self.redirect_uri
        params = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if self.client_secret:
            params["client_secret"] = self.client_secret
        code_verifier = kwargs.get("code_verifier")
        if code_verifier:
            params["code_verifier"] = code_verifier
        return params

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        params = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": credential.refresh_token or "",
        }
        if self.client_secret:
            params["client_secret"] = self.client_secret
        if self._scope_on_refresh:
            scopes = list(credential.scopes) if credential.scopes else list(self.scopes)
            params["scope"] = self._scope_sep.join(scopes)
        return params

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        if not self.REVOKE_ENDPOINT:
            # RFC 7009 is optional; providers without it succeed silently.
            return True
        token = credential.refresh_token or credential.access_token
        if not token:
            return False
        async with self._get_client() as client:
            try:
                response = await client.post(self.REVOKE_ENDPOINT, data={"token": token})
                response.raise_for_status()
                return True
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        """Generic validate via RFC 7662 introspection when available.

        Without an introspection endpoint we cannot verify the token server-side
        in a standards-compliant way, so return True (optimistic) rather than
        calling vendor-specific endpoints. Subclasses override for vendors that
        expose a non-standard validation endpoint.
        """
        if not self.INTROSPECTION_ENDPOINT:
            return True
        async with self._get_client() as client:
            try:
                # httpx.BasicAuth returns an ``Auth`` instance; passing a bare
                # tuple here type-errors under strict mypy because
                # ``tuple[str, str]`` is invariant and not a subtype of the
                # declared ``tuple[str | bytes, str | bytes]``.
                auth = (
                    httpx.BasicAuth(self.client_id, self.client_secret)
                    if self.client_secret
                    else None
                )
                response = await client.post(
                    self.INTROSPECTION_ENDPOINT,
                    data={"token": access_token},
                    auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                )
                response.raise_for_status()
                body = response.json()
                return bool(body.get("active"))
            except Exception:
                return False
