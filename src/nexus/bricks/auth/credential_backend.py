"""Pluggable credential backends for the unified auth-profile store.

Architecture (epic #3722, decision 1A):
  AuthProfile holds routing metadata (provider, backend, backend_key).
  CredentialBackend implementations resolve the opaque backend_key into an
  actual credential (access token, API key, etc.).

This module defines:
  - ResolvedCredential: the output of a backend resolve() call.
  - BackendHealth: health check result for a backend + key pair.
  - CredentialBackend: Protocol that all backends implement.
  - NexusTokenManagerBackend: wraps the TokenResolver protocol from
    token_resolver.py (Phase 0, #3737) — the only backend in Phase 1.

Phase 2 (#3739) adds external-CLI adapters: AwsCliBackend, GcloudBackend, etc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolved credential — output of backend.resolve()
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """A freshly resolved credential from a backend.

    The ``kind`` discriminator tells callers how to use the credential:
      - "api_key": use ``api_key`` field.
      - "bearer_token": use ``access_token`` field (OAuth / JWT).
    """

    kind: str  # "api_key", "bearer_token"
    api_key: str | None = None
    access_token: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Backend health
# ---------------------------------------------------------------------------


class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BackendHealth:
    """Result of a backend health check for a specific key."""

    status: HealthStatus
    message: str = ""
    checked_at: datetime | None = None


# ---------------------------------------------------------------------------
# CredentialBackend protocol (decision 4A: async-only resolve)
# ---------------------------------------------------------------------------


@runtime_checkable
class CredentialBackend(Protocol):
    """Protocol for pluggable credential resolution backends.

    Each implementation knows how to turn an opaque ``backend_key`` into a
    usable credential. The backend owns all complexity (refresh, rotation,
    subprocess calls, caching) — callers just call ``resolve()``.

    ``resolve()`` is async-only (decision 4A). ``select()`` on the store is
    cache-only and never calls resolve(). Actual resolution happens in async
    contexts (pool.execute, background sync loop).
    """

    @property
    def name(self) -> str:
        """Stable identifier for this backend type, e.g. 'nexus-token-manager'."""
        ...

    async def resolve(self, backend_key: str) -> ResolvedCredential:
        """Resolve the opaque key into a usable credential.

        Must return a credential that is valid right now. Refreshing, rotating,
        and caching are the backend's responsibility.

        Args:
            backend_key: Opaque key stored in AuthProfile.backend_key.

        Raises:
            CredentialResolutionError: if the credential cannot be resolved.
        """
        ...

    async def health_check(self, backend_key: str) -> BackendHealth:
        """Check whether the backend can resolve this key.

        Non-destructive, best-effort. Used by ``auth doctor``.
        """
        ...


class CredentialResolutionError(Exception):
    """Raised when a CredentialBackend cannot resolve a credential."""

    def __init__(self, backend: str, backend_key: str, reason: str) -> None:
        self.backend = backend
        self.backend_key = backend_key
        self.reason = reason
        super().__init__(f"[{backend}] cannot resolve '{backend_key}': {reason}")


# ---------------------------------------------------------------------------
# NexusTokenManagerBackend — wraps TokenResolver from #3737
# ---------------------------------------------------------------------------


class NexusTokenManagerBackend:
    """CredentialBackend that delegates to the TokenResolver protocol.

    The backend_key format is ``"{provider}/{user_email}"`` or
    ``"{provider}/{user_email}/{zone_id}"``. This thin wrapper parses the
    key and calls ``TokenResolver.resolve()``.

    The TokenResolver (implemented by TokenManager) handles all RFC 9700
    rotation, reuse detection, encryption, and rate limiting internally.
    """

    _NAME = "nexus-token-manager"

    def __init__(self, token_resolver: _TokenResolverLike) -> None:
        self._resolver = token_resolver

    @property
    def name(self) -> str:
        return self._NAME

    @staticmethod
    def make_backend_key(
        provider: str,
        user_email: str,
        zone_id: str | None = None,
    ) -> str:
        """Build a backend_key from its components."""
        if zone_id:
            return f"{provider}/{user_email}/{zone_id}"
        return f"{provider}/{user_email}"

    @staticmethod
    def parse_backend_key(backend_key: str) -> tuple[str, str, str | None]:
        """Parse backend_key into (provider, user_email, zone_id | None)."""
        parts = backend_key.split("/", 2)
        if len(parts) < 2:
            raise CredentialResolutionError(
                NexusTokenManagerBackend._NAME,
                backend_key,
                f"expected 'provider/user_email[/zone_id]', got {backend_key!r}",
            )
        provider = parts[0]
        user_email = parts[1]
        zone_id = parts[2] if len(parts) > 2 else None
        return provider, user_email, zone_id

    async def resolve(self, backend_key: str) -> ResolvedCredential:
        provider, user_email, zone_id = self.parse_backend_key(backend_key)
        try:
            kwargs: dict = {"provider": provider, "user_email": user_email}
            if zone_id is not None:
                kwargs["zone_id"] = zone_id
            resolved = await self._resolver.resolve(**kwargs)
        except Exception as exc:
            raise CredentialResolutionError(self._NAME, backend_key, str(exc)) from exc

        return ResolvedCredential(
            kind="bearer_token",
            access_token=resolved.access_token,
            expires_at=resolved.expires_at,
            scopes=resolved.scopes,
        )

    async def health_check(self, backend_key: str) -> BackendHealth:
        """Check backend health by resolving the credential.

        NOTE: This calls resolve(), which may trigger a token refresh. For a
        truly read-only health probe, Phase 2 should add a separate method
        that inspects stored metadata without refreshing.
        """

        now = datetime.now(UTC)
        try:
            provider, user_email, zone_id = self.parse_backend_key(backend_key)
            kwargs: dict = {"provider": provider, "user_email": user_email}
            if zone_id is not None:
                kwargs["zone_id"] = zone_id
            resolved = await self._resolver.resolve(**kwargs)
            if resolved.expires_at:
                # Normalize to timezone-aware UTC for safe comparison
                expires = resolved.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires < now:
                    return BackendHealth(
                        status=HealthStatus.DEGRADED,
                        message="Token resolved but already expired",
                        checked_at=now,
                    )
            return BackendHealth(
                status=HealthStatus.HEALTHY,
                message="Token resolved successfully",
                checked_at=now,
            )
        except Exception as exc:
            return BackendHealth(
                status=HealthStatus.UNHEALTHY,
                message=str(exc),
                checked_at=now,
            )


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------


class CredentialBackendRegistry:
    """Maps backend names to backend instances.

    Used by the credential pool to resolve credentials given an AuthProfile's
    backend + backend_key pair.
    """

    def __init__(self) -> None:
        self._backends: dict[str, CredentialBackend] = {}

    def register(self, backend: CredentialBackend) -> None:
        self._backends[backend.name] = backend

    def get(self, name: str) -> CredentialBackend | None:
        return self._backends.get(name)

    def list_backends(self) -> list[str]:
        return list(self._backends.keys())


# ---------------------------------------------------------------------------
# Private type alias for TokenResolver duck typing
# ---------------------------------------------------------------------------


# We use a Protocol here rather than importing TokenResolver directly to
# avoid a circular dependency (token_resolver imports from contracts).
class _ResolvedTokenLike(Protocol):
    access_token: str
    expires_at: datetime | None
    scopes: tuple[str, ...]


class _TokenResolverLike(Protocol):
    async def resolve(
        self,
        provider: str,
        user_email: str,
        *,
        zone_id: str = "root",
    ) -> _ResolvedTokenLike: ...
