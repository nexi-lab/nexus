"""Tests for RFC 8414 + OIDC Discovery client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nexus.lib.oauth.discovery import DiscoveryClient, DiscoveryError, DiscoveryMetadata

_SAMPLE_SCOPES = ("read", "write", "openid")
_SAMPLE_METADATA: dict[str, Any] = {
    "issuer": "https://issuer.example",
    "authorization_endpoint": "https://issuer.example/oauth2/authorize",
    "token_endpoint": "https://issuer.example/oauth2/token",
    "revocation_endpoint": "https://issuer.example/oauth2/revoke",
    "registration_endpoint": "https://issuer.example/oauth2/register",
    "scopes_supported": list(_SAMPLE_SCOPES),
    "response_types_supported": ["code"],
    "code_challenge_methods_supported": ["S256", "plain"],
}


@pytest.mark.asyncio
async def test_fetch_parses_well_known_oauth_authorization_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-authorization-server"
        return httpx.Response(200, json=_SAMPLE_METADATA)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        meta = await DiscoveryClient(client=client).fetch("https://issuer.example")

    assert isinstance(meta, DiscoveryMetadata)
    assert meta.authorization_endpoint == _SAMPLE_METADATA["authorization_endpoint"]
    assert meta.token_endpoint == _SAMPLE_METADATA["token_endpoint"]
    assert meta.revocation_endpoint == _SAMPLE_METADATA["revocation_endpoint"]
    assert meta.scopes_supported == _SAMPLE_SCOPES
    assert "S256" in meta.code_challenge_methods_supported


@pytest.mark.asyncio
async def test_fetch_falls_back_to_openid_configuration() -> None:
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.url.path)
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(404)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=_SAMPLE_METADATA)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        meta = await DiscoveryClient(client=client).fetch("https://issuer.example")

    assert call_log == [
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ]
    assert meta.token_endpoint == _SAMPLE_METADATA["token_endpoint"]


@pytest.mark.asyncio
async def test_fetch_rejects_issuer_mismatch() -> None:
    payload = dict(_SAMPLE_METADATA, issuer="https://other.example")

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DiscoveryError) as exc_info:
            await DiscoveryClient(client=client).fetch("https://issuer.example")
    assert "issuer" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_fetch_times_out_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise httpx.ConnectTimeout("simulated")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DiscoveryError):
            await DiscoveryClient(client=client, timeout=0.1).fetch("https://issuer.example")


def test_metadata_rejects_missing_required_fields() -> None:
    with pytest.raises(DiscoveryError):
        DiscoveryMetadata.from_dict({"issuer": "x"})  # missing endpoints
