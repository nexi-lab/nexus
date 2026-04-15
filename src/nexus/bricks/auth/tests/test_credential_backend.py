"""Tests for CredentialBackend protocol and NexusTokenManagerBackend.

Coverage map:
  - CredentialBackend protocol conformance
  - NexusTokenManagerBackend.parse_backend_key: valid 2-part, 3-part, invalid
  - NexusTokenManagerBackend.make_backend_key: round-trip with parse
  - NexusTokenManagerBackend.resolve: happy path, resolver error
  - NexusTokenManagerBackend.health_check: healthy, expired, unhealthy
  - CredentialBackendRegistry: register, get, list
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nexus.bricks.auth.credential_backend import (
    CredentialBackend,
    CredentialBackendRegistry,
    CredentialResolutionError,
    HealthStatus,
    NexusTokenManagerBackend,
)
from nexus.bricks.auth.oauth.token_resolver import ResolvedToken

# ---------------------------------------------------------------------------
# Fake TokenResolver for tests
# ---------------------------------------------------------------------------


class FakeTokenResolver:
    """Minimal TokenResolver-like for testing NexusTokenManagerBackend."""

    def __init__(
        self,
        *,
        token: str = "fake-access-token",
        expires_at: datetime | None = None,
        scopes: tuple[str, ...] = (),
        fail: bool = False,
    ) -> None:
        self._token = token
        self._expires_at = expires_at
        self._scopes = scopes
        self._fail = fail
        self.resolve_calls: list[dict] = []

    async def resolve(
        self, provider: str, user_email: str, *, zone_id: str = "root"
    ) -> ResolvedToken:
        self.resolve_calls.append(
            {"provider": provider, "user_email": user_email, "zone_id": zone_id}
        )
        if self._fail:
            raise RuntimeError("resolver failed")
        return ResolvedToken(
            access_token=self._token,
            expires_at=self._expires_at,
            scopes=self._scopes,
        )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_nexus_backend_satisfies_protocol(self) -> None:
        resolver = FakeTokenResolver()
        backend = NexusTokenManagerBackend(resolver)
        assert isinstance(backend, CredentialBackend)

    def test_name_property(self) -> None:
        resolver = FakeTokenResolver()
        backend = NexusTokenManagerBackend(resolver)
        assert backend.name == "nexus-token-manager"


# ---------------------------------------------------------------------------
# Backend key parsing
# ---------------------------------------------------------------------------


class TestBackendKeyParsing:
    def test_parse_two_parts(self) -> None:
        provider, email, zone = NexusTokenManagerBackend.parse_backend_key(
            "google/alice@example.com"
        )
        assert provider == "google"
        assert email == "alice@example.com"
        assert zone is None

    def test_parse_three_parts(self) -> None:
        provider, email, zone = NexusTokenManagerBackend.parse_backend_key(
            "google/alice@example.com/zone-42"
        )
        assert provider == "google"
        assert email == "alice@example.com"
        assert zone == "zone-42"

    def test_parse_invalid_key(self) -> None:
        with pytest.raises(CredentialResolutionError, match="expected"):
            NexusTokenManagerBackend.parse_backend_key("no-slash")

    def test_make_and_parse_round_trip(self) -> None:
        key = NexusTokenManagerBackend.make_backend_key("openai", "bob@co.com", "z1")
        provider, email, zone = NexusTokenManagerBackend.parse_backend_key(key)
        assert provider == "openai"
        assert email == "bob@co.com"
        assert zone == "z1"

    def test_make_without_zone(self) -> None:
        key = NexusTokenManagerBackend.make_backend_key("openai", "bob@co.com")
        assert key == "openai/bob@co.com"


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


class TestResolve:
    async def test_resolve_happy_path(self) -> None:
        resolver = FakeTokenResolver(
            token="real-token",
            expires_at=datetime(2030, 1, 1),
            scopes=("read", "write"),
        )
        backend = NexusTokenManagerBackend(resolver)
        cred = await backend.resolve("openai/alice@test.com")

        assert cred.kind == "bearer_token"
        assert cred.access_token == "real-token"
        assert cred.expires_at == datetime(2030, 1, 1)
        assert cred.scopes == ("read", "write")

        # Verify resolver was called with correct args
        assert len(resolver.resolve_calls) == 1
        assert resolver.resolve_calls[0]["provider"] == "openai"
        assert resolver.resolve_calls[0]["user_email"] == "alice@test.com"

    async def test_resolve_with_zone(self) -> None:
        resolver = FakeTokenResolver(token="zoned")
        backend = NexusTokenManagerBackend(resolver)
        cred = await backend.resolve("google/bob@co.com/zone-x")

        assert cred.access_token == "zoned"
        assert resolver.resolve_calls[0]["zone_id"] == "zone-x"

    async def test_resolve_error_wraps_in_credential_resolution_error(self) -> None:
        resolver = FakeTokenResolver(fail=True)
        backend = NexusTokenManagerBackend(resolver)

        with pytest.raises(CredentialResolutionError, match="resolver failed"):
            await backend.resolve("openai/alice@test.com")


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        resolver = FakeTokenResolver(
            token="good",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        backend = NexusTokenManagerBackend(resolver)
        health = await backend.health_check("openai/alice@test.com")
        assert health.status == HealthStatus.HEALTHY

    async def test_health_check_degraded_expired(self) -> None:
        resolver = FakeTokenResolver(
            token="expired",
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        backend = NexusTokenManagerBackend(resolver)
        health = await backend.health_check("openai/alice@test.com")
        assert health.status == HealthStatus.DEGRADED

    async def test_health_check_unhealthy_on_error(self) -> None:
        resolver = FakeTokenResolver(fail=True)
        backend = NexusTokenManagerBackend(resolver)
        health = await backend.health_check("openai/alice@test.com")
        assert health.status == HealthStatus.UNHEALTHY


# ---------------------------------------------------------------------------
# CredentialBackendRegistry
# ---------------------------------------------------------------------------


class TestBackendRegistry:
    def test_register_and_get(self) -> None:
        registry = CredentialBackendRegistry()
        backend = NexusTokenManagerBackend(FakeTokenResolver())
        registry.register(backend)
        assert registry.get("nexus-token-manager") is backend

    def test_get_unknown(self) -> None:
        registry = CredentialBackendRegistry()
        assert registry.get("nope") is None

    def test_list_backends(self) -> None:
        registry = CredentialBackendRegistry()
        registry.register(NexusTokenManagerBackend(FakeTokenResolver()))
        assert "nexus-token-manager" in registry.list_backends()
