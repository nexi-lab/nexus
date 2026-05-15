"""ExternalCliBackend — CredentialBackend for external CLI credentials.

Implements the CredentialBackend protocol. resolve() delegates to the
appropriate adapter's resolve_credential() for a fresh read. Never
persists external credentials on nexus-side disk.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nexus.bricks.auth.credential_backend import (
    BackendHealth,
    CredentialResolutionError,
    HealthStatus,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.registry import AdapterRegistry


class ExternalCliBackend:
    """CredentialBackend for external-CLI-managed credentials."""

    _NAME = "external-cli"

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return self._NAME

    async def resolve(self, backend_key: str) -> ResolvedCredential:
        """Parse adapter_name from key, delegate to adapter.resolve_credential()."""
        adapter_name, _ = self._parse_key(backend_key)
        adapter = self._registry.get_adapter(adapter_name)
        if adapter is None:
            raise CredentialResolutionError(
                self._NAME, backend_key, f"no adapter registered for '{adapter_name}'"
            )
        return await adapter.resolve_credential(backend_key)

    def resolve_sync(self, backend_key: str) -> ResolvedCredential:
        """Synchronous variant of resolve() for sync calling contexts."""
        adapter_name, _ = self._parse_key(backend_key)
        adapter = self._registry.get_adapter(adapter_name)
        if adapter is None:
            raise CredentialResolutionError(
                self._NAME, backend_key, f"no adapter registered for '{adapter_name}'"
            )
        return adapter.resolve_credential_sync(backend_key)

    async def health_check(self, backend_key: str) -> BackendHealth:
        """Non-destructive: try resolve, report healthy/unhealthy."""
        now = datetime.now(UTC)
        try:
            await self.resolve(backend_key)
            return BackendHealth(
                status=HealthStatus.HEALTHY,
                message="Credential resolved successfully",
                checked_at=now,
            )
        except Exception as exc:
            return BackendHealth(
                status=HealthStatus.UNHEALTHY,
                message=str(exc),
                checked_at=now,
            )

    @staticmethod
    def _parse_key(backend_key: str) -> tuple[str, str]:
        parts = backend_key.split("/", 1)
        if len(parts) < 2:
            raise CredentialResolutionError(
                ExternalCliBackend._NAME,
                backend_key,
                f"expected 'adapter_name/profile', got {backend_key!r}",
            )
        return parts[0], parts[1]
