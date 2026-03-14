"""Regression tests for open-access loopback restriction — Issue #2960 H4.

Verifies that open-access mode (no API key, no auth provider) rejects
header-based identity from non-loopback client addresses.
"""

from types import SimpleNamespace

import pytest

from nexus.server.dependencies import _is_loopback, resolve_auth


class TestIsLoopback:
    """Unit tests for the _is_loopback helper."""

    def test_ipv4_loopback(self) -> None:
        assert _is_loopback("127.0.0.1") is True

    def test_ipv6_loopback(self) -> None:
        assert _is_loopback("::1") is True

    def test_localhost(self) -> None:
        assert _is_loopback("localhost") is True

    def test_remote_ip_rejected(self) -> None:
        assert _is_loopback("192.168.1.100") is False

    def test_public_ip_rejected(self) -> None:
        assert _is_loopback("8.8.8.8") is False

    def test_none_rejected(self) -> None:
        assert _is_loopback(None) is False

    def test_empty_string_rejected(self) -> None:
        assert _is_loopback("") is False

    def test_ipv4_loopback_range(self) -> None:
        """127.0.0.0/8 is all loopback — not just 127.0.0.1."""
        assert _is_loopback("127.0.0.2") is True
        assert _is_loopback("127.255.255.255") is True

    def test_ipv4_mapped_ipv6_loopback(self) -> None:
        """::ffff:127.0.0.1 is IPv4-mapped IPv6 loopback."""
        assert _is_loopback("::ffff:127.0.0.1") is True

    def test_ipv4_mapped_ipv6_non_loopback(self) -> None:
        assert _is_loopback("::ffff:192.168.1.1") is False


class TestOpenAccessLoopbackRestriction:
    """Regression: H4 — open access mode trusts headers from any client."""

    @pytest.mark.asyncio
    async def test_open_access_allows_loopback(self) -> None:
        """Open access from 127.0.0.1 should succeed."""
        state = SimpleNamespace(api_key=None, auth_provider=None)
        result = await resolve_auth(
            app_state=state,
            x_nexus_subject="user:alice",
            client_host="127.0.0.1",
        )
        assert result is not None
        assert result["authenticated"] is True
        assert result["subject_id"] == "alice"

    @pytest.mark.asyncio
    async def test_open_access_rejects_remote_ip(self) -> None:
        """Open access from a remote IP must return None (unauthenticated)."""
        state = SimpleNamespace(api_key=None, auth_provider=None)
        result = await resolve_auth(
            app_state=state,
            x_nexus_subject="user:attacker",
            client_host="192.168.1.100",
        )
        assert result is None, "Remote client should be rejected in open-access mode"

    @pytest.mark.asyncio
    async def test_open_access_rejects_none_client_host(self) -> None:
        """If client_host is unknown, reject in open-access mode."""
        state = SimpleNamespace(api_key=None, auth_provider=None)
        result = await resolve_auth(
            app_state=state,
            x_nexus_subject="user:someone",
            client_host=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_auth_ignores_client_host(self) -> None:
        """When API key auth is configured, client_host doesn't matter."""
        state = SimpleNamespace(api_key="test-key-123", auth_provider=None)
        result = await resolve_auth(
            app_state=state,
            authorization="Bearer test-key-123",
            client_host="8.8.8.8",  # remote, but auth is configured
        )
        assert result is not None
        assert result["authenticated"] is True
