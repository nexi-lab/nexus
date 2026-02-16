"""OAuth management domain client (sync + async).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

import builtins
from typing import Any

from nexus.constants import DEFAULT_OAUTH_REDIRECT_URI


class OAuthClient:
    """OAuth management domain client (sync)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    def list_providers(self) -> builtins.list[dict[str, Any]]:
        return self._call_rpc("oauth_list_providers", {})  # type: ignore[no-any-return]

    def get_auth_url(
        self,
        provider: str,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        scopes: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "provider": provider,
            "redirect_uri": redirect_uri,
        }
        if scopes is not None:
            params["scopes"] = scopes
        return self._call_rpc("oauth_get_auth_url", params)  # type: ignore[no-any-return]

    def exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        code_verifier: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "provider": provider,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if user_email is not None:
            params["user_email"] = user_email
        if state is not None:
            params["state"] = state
        if code_verifier is not None:
            params["code_verifier"] = code_verifier
        return self._call_rpc("oauth_exchange_code", params)  # type: ignore[no-any-return]

    def list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"include_revoked": include_revoked}
        if provider is not None:
            params["provider"] = provider
        return self._call_rpc("oauth_list_credentials", params)  # type: ignore[no-any-return]

    def revoke_credential(
        self,
        provider: str,
        user_email: str,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "oauth_revoke_credential",
            {"provider": provider, "user_email": user_email},
        )

    def test_credential(
        self,
        provider: str,
        user_email: str,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "oauth_test_credential",
            {"provider": provider, "user_email": user_email},
        )


class AsyncOAuthClient:
    """OAuth management domain client (async)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    async def list_providers(self) -> builtins.list[dict[str, Any]]:
        return await self._call_rpc("oauth_list_providers", {})  # type: ignore[no-any-return]

    async def get_auth_url(
        self,
        provider: str,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        scopes: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "provider": provider,
            "redirect_uri": redirect_uri,
        }
        if scopes is not None:
            params["scopes"] = scopes
        return await self._call_rpc("oauth_get_auth_url", params)  # type: ignore[no-any-return]

    async def exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        code_verifier: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "provider": provider,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if user_email is not None:
            params["user_email"] = user_email
        if state is not None:
            params["state"] = state
        if code_verifier is not None:
            params["code_verifier"] = code_verifier
        return await self._call_rpc("oauth_exchange_code", params)  # type: ignore[no-any-return]

    async def list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"include_revoked": include_revoked}
        if provider is not None:
            params["provider"] = provider
        return await self._call_rpc("oauth_list_credentials", params)  # type: ignore[no-any-return]

    async def revoke_credential(
        self,
        provider: str,
        user_email: str,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "oauth_revoke_credential",
            {"provider": provider, "user_email": user_email},
        )

    async def test_credential(
        self,
        provider: str,
        user_email: str,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "oauth_test_credential",
            {"provider": provider, "user_email": user_email},
        )
