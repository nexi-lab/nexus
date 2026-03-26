"""Auth service — core business logic (Decision #7).

Extracts business logic from route handlers into a testable service.
Route handlers become thin: validate input -> service.method() -> format response.
"""

import logging
from typing import Any

from nexus.bricks.auth.cache import AuthCache
from nexus.bricks.auth.providers.base import AuthProvider, AuthResult
from nexus.bricks.auth.zone_helpers import (
    create_zone,
    get_zone_strategy_from_email,
    suggest_zone_id,
)

logger = logging.getLogger(__name__)


class AuthService:
    """Core auth business logic, backed by an AuthProvider and AuthCache.

    This service is the single entry point for authentication operations
    in the Auth brick. It wraps provider calls with caching and provides
    high-level zone setup and user management methods.
    """

    def __init__(
        self,
        provider: AuthProvider,
        cache: AuthCache | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache or AuthCache()

    @property
    def provider(self) -> AuthProvider:
        """Access the underlying auth provider."""
        return self._provider

    @property
    def cache(self) -> AuthCache:
        """Access the auth cache."""
        return self._cache

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate a token (cache-aware, singleflight).

        Checks the cache first; on miss, delegates to the provider
        and caches the result.  Concurrent calls for the same token
        are coalesced via singleflight (Issue #15).
        """
        if not token:
            return AuthResult(authenticated=False)

        async def _fetch() -> dict[str, Any] | None:
            result = await self._provider.authenticate(token)
            if not result.authenticated:
                return None
            return {
                "authenticated": result.authenticated,
                "subject_type": result.subject_type,
                "subject_id": result.subject_id,
                "zone_id": result.zone_id,
                "is_admin": result.is_admin,
                "metadata": result.metadata,
                "agent_generation": result.agent_generation,
                "inherit_permissions": result.inherit_permissions,
            }

        cached = await self._cache.get_or_fetch(token, _fetch)
        if cached is None:
            return AuthResult(authenticated=False)
        return AuthResult(
            **{k: v for k, v in cached.items() if k in AuthResult.__dataclass_fields__}
        )

    async def validate_token(self, token: str) -> bool:
        """Quick token validation."""
        return await self._provider.validate_token(token)

    def invalidate_cached_token(self, token: str) -> None:
        """Remove a token from the cache (Decision #15)."""
        self._cache.invalidate(token)

    def setup_zone(
        self,
        session: Any,
        email: str,
        zone_id_override: str | None = None,
        zone_name_override: str | None = None,
    ) -> dict[str, Any]:
        """Set up a zone based on email domain strategy.

        Returns:
            Dict with zone_id, zone_name, is_personal, domain.
        """
        base_slug, zone_name_base, domain, is_personal = get_zone_strategy_from_email(email)

        zone_id = zone_id_override or suggest_zone_id(base_slug, session)
        zone_name = zone_name_override or zone_name_base

        zone = create_zone(
            session=session,
            zone_id=zone_id,
            name=zone_name,
            domain=domain,
        )

        return {
            "zone_id": zone.zone_id,
            "zone_name": zone.name,
            "is_personal": is_personal,
            "domain": domain,
        }

    def initialize(self) -> None:
        """Brick lifecycle: startup initialization."""
        self.verify_imports()

    def shutdown(self) -> None:
        """Brick lifecycle: graceful shutdown."""
        self._provider.close()
        self._cache.clear()

    def close(self) -> None:
        """Alias for shutdown()."""
        self.shutdown()

    def verify_imports(self) -> dict[str, bool]:
        """Validate required and optional module imports."""
        from nexus.bricks.auth.manifest import verify_imports as _verify

        return _verify()
