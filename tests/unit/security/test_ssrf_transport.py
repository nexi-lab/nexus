"""Tests for PinnedResolverTransport and make_pinned_client_factory (Issue #3792).

The transport pins TCP connect to IPs captured at validation time. These
tests cover the observable surface:
  - factory signature and configured client options (timeout, follow_redirects)
  - transport rejects empty pinned IP lists
  - transport uses a pinned httpcore network backend that refuses fresh DNS

The actual pin enforcement is tested by patching the underlying
``httpcore.AsyncNetworkBackend`` connect method and asserting the host
it was called with.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from nexus.lib.security.ssrf_transport import (
    PinnedResolverTransport,
    make_pinned_client_factory,
)
from nexus.lib.security.url_validator import ValidatedURL


@pytest.fixture
def validated_url() -> ValidatedURL:
    return ValidatedURL(
        url="https://example.com/",
        resolved_ips=("93.184.216.34",),
        hostname="example.com",
    )


class TestPinnedResolverTransportConstruction:
    def test_requires_pinned_ips(self) -> None:
        empty = ValidatedURL(url="https://x/", resolved_ips=(), hostname="x")
        with pytest.raises(ValueError, match="at least one pinned IP"):
            PinnedResolverTransport(empty)

    def test_stores_validated(self, validated_url: ValidatedURL) -> None:
        t = PinnedResolverTransport(validated_url)
        assert t.pinned_ips == ["93.184.216.34"]
        assert t.server_hostname == "example.com"


class TestPinnedResolverBackend:
    """The network backend substitutes the pinned IP for the request host."""

    @pytest.mark.asyncio
    async def test_connect_uses_pinned_ip(self, validated_url: ValidatedURL) -> None:
        captured: dict[str, object] = {}

        async def fake_connect_tcp(
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options=None,
            **kw,
        ):
            captured["host"] = host
            captured["port"] = port
            raise httpx.ConnectError("stop here")

        transport = PinnedResolverTransport(validated_url)

        with patch.object(transport.network_backend, "connect_tcp", side_effect=fake_connect_tcp):
            async with httpx.AsyncClient(transport=transport) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get("https://example.com/")

        assert captured["host"] == "93.184.216.34"

    @pytest.mark.asyncio
    async def test_multi_ip_failover_without_fresh_dns(self) -> None:
        validated = ValidatedURL(
            url="https://example.com/",
            resolved_ips=("93.184.216.34", "93.184.216.35"),
            hostname="example.com",
        )
        transport = PinnedResolverTransport(validated)
        attempts: list[str] = []

        async def fake_connect_tcp(host: str, port: int, **kw):
            attempts.append(host)
            raise httpx.ConnectError("simulated")

        with patch.object(transport.network_backend, "connect_tcp", side_effect=fake_connect_tcp):
            async with httpx.AsyncClient(transport=transport) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get("https://example.com/")

        assert all(a in {"93.184.216.34", "93.184.216.35"} for a in attempts)
        assert len(attempts) >= 1


class TestMakePinnedClientFactory:
    """Factory matches McpHttpClientFactory signature and returns an
    AsyncClient wired to PinnedResolverTransport with redirects off."""

    @pytest.mark.asyncio
    async def test_factory_signature_is_mcp_compatible(self, validated_url: ValidatedURL) -> None:
        factory = make_pinned_client_factory(validated_url)
        client = factory(headers={"X-Test": "1"}, timeout=None, auth=None)
        try:
            assert isinstance(client, httpx.AsyncClient)
            assert client.headers["X-Test"] == "1"
            assert client.follow_redirects is False
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_factory_accepts_timeout_and_auth(self, validated_url: ValidatedURL) -> None:
        factory = make_pinned_client_factory(validated_url)
        client = factory(
            headers=None,
            timeout=httpx.Timeout(5.0),
            auth=httpx.BasicAuth("u", "p"),
        )
        try:
            assert client.timeout.connect == 5.0
        finally:
            await client.aclose()
