"""Tests for ExternalCliBackend.

Covers:
  - resolve() delegates to the correct adapter
  - resolve() raises CredentialResolutionError for unknown adapters / profiles
  - health_check() returns HEALTHY / UNHEALTHY
  - name property
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    HealthStatus,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncResult,
)
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
from nexus.bricks.auth.external_sync.registry import AdapterRegistry
from nexus.bricks.auth.profile import InMemoryAuthProfileStore

# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class _MockAdapter(ExternalCliSyncAdapter):
    """Adapter that returns pre-configured credentials by backend_key."""

    adapter_name = "mock"
    sync_ttl_seconds = 60.0
    failure_threshold = 3
    reset_timeout_seconds = 60.0

    def __init__(self, credentials: dict[str, ResolvedCredential]) -> None:
        self._credentials = credentials

    async def detect(self) -> bool:
        return True

    async def sync(self) -> SyncResult:
        return SyncResult(adapter_name=self.adapter_name)

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        if backend_key not in self._credentials:
            raise CredentialResolutionError(
                "mock", backend_key, f"no credential for key '{backend_key}'"
            )
        return self._credentials[backend_key]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(adapters: list[ExternalCliSyncAdapter]) -> AdapterRegistry:
    store = InMemoryAuthProfileStore()
    return AdapterRegistry(adapters, store)


_SAMPLE_CRED = ResolvedCredential(
    kind="api_key",
    api_key="test-secret-key",
    expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    metadata={"region": "us-east-1"},
)


# ---------------------------------------------------------------------------
# TestExternalCliBackendResolve
# ---------------------------------------------------------------------------


class TestExternalCliBackendResolve:
    async def test_resolve_delegates_to_adapter(self) -> None:
        adapter = _MockAdapter(credentials={"mock/default": _SAMPLE_CRED})
        registry = _make_registry([adapter])
        backend = ExternalCliBackend(registry)

        result = await backend.resolve("mock/default")

        assert result.kind == "api_key"
        assert result.api_key == "test-secret-key"
        assert result.expires_at == datetime(2099, 1, 1, tzinfo=UTC)
        assert result.metadata == {"region": "us-east-1"}

    async def test_resolve_unknown_adapter_raises(self) -> None:
        registry = _make_registry([])
        backend = ExternalCliBackend(registry)

        with pytest.raises(CredentialResolutionError, match="no adapter"):
            await backend.resolve("nonexistent/profile")

    async def test_resolve_unknown_profile_raises(self) -> None:
        adapter = _MockAdapter(credentials={"mock/default": _SAMPLE_CRED})
        registry = _make_registry([adapter])
        backend = ExternalCliBackend(registry)

        with pytest.raises(CredentialResolutionError, match="no credential"):
            await backend.resolve("mock/unknown-profile")

    async def test_resolve_malformed_key_raises(self) -> None:
        registry = _make_registry([])
        backend = ExternalCliBackend(registry)

        with pytest.raises(CredentialResolutionError, match="expected 'adapter_name/profile'"):
            await backend.resolve("no-slash-here")


# ---------------------------------------------------------------------------
# TestExternalCliBackendHealthCheck
# ---------------------------------------------------------------------------


class TestExternalCliBackendHealthCheck:
    async def test_health_check_healthy(self) -> None:
        adapter = _MockAdapter(credentials={"mock/default": _SAMPLE_CRED})
        registry = _make_registry([adapter])
        backend = ExternalCliBackend(registry)

        health = await backend.health_check("mock/default")

        assert health.status == HealthStatus.HEALTHY
        assert "resolved successfully" in health.message
        assert health.checked_at is not None

    async def test_health_check_unhealthy_on_missing(self) -> None:
        adapter = _MockAdapter(credentials={})
        registry = _make_registry([adapter])
        backend = ExternalCliBackend(registry)

        health = await backend.health_check("mock/missing")

        assert health.status == HealthStatus.UNHEALTHY
        assert health.checked_at is not None


# ---------------------------------------------------------------------------
# TestExternalCliBackendName
# ---------------------------------------------------------------------------


class TestExternalCliBackendName:
    def test_name_returns_external_cli(self) -> None:
        registry = _make_registry([])
        backend = ExternalCliBackend(registry)
        assert backend.name == "external-cli"
