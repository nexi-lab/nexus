"""Tests for synchronous credential resolution path."""

from __future__ import annotations

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError, ResolvedCredential
from nexus.bricks.auth.external_sync.base import ExternalCliSyncAdapter, SyncResult
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
from nexus.bricks.auth.external_sync.registry import AdapterRegistry
from nexus.bricks.auth.profile import InMemoryAuthProfileStore


class _StubAdapter(ExternalCliSyncAdapter):
    adapter_name = "stub"

    async def sync(self) -> SyncResult:
        return SyncResult(adapter_name="stub")

    async def detect(self) -> bool:
        return True

    async def resolve_credential(self, _backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="bearer_token", access_token="async-tok")

    def resolve_credential_sync(self, _backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="bearer_token", access_token="sync-tok")


class TestResolveSyncBackend:
    def test_resolve_sync_delegates_to_adapter(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry([_StubAdapter()], store)
        backend = ExternalCliBackend(registry)

        cred = backend.resolve_sync("stub/my-account")

        assert cred.kind == "bearer_token"
        assert cred.access_token == "sync-tok"

    def test_resolve_sync_unknown_adapter_raises(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry([], store)
        backend = ExternalCliBackend(registry)

        with pytest.raises(CredentialResolutionError, match="no adapter"):
            backend.resolve_sync("unknown/account")

    def test_resolve_sync_bad_key_format_raises(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry([_StubAdapter()], store)
        backend = ExternalCliBackend(registry)

        with pytest.raises(CredentialResolutionError, match="expected"):
            backend.resolve_sync("no-slash")
