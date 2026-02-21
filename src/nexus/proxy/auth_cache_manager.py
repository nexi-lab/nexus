"""Auth cache manager for edge offline mode.

Wraps the existing ``AuthCache`` with edge-specific grace period handling.
During offline mode, extends TTL with a configurable grace period.
On reconnect, forces token refresh before any data operations.

Issue #1707: Edge split-brain resilience.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.auth.cache import AuthCache

logger = logging.getLogger(__name__)


class AuthCacheManager:
    """Edge-aware auth cache wrapper with offline grace period.

    Parameters
    ----------
    auth_cache:
        Underlying ``AuthCache`` instance (injected, not owned).
    grace_period_seconds:
        How long cached auth tokens remain valid during offline mode.
    """

    def __init__(
        self,
        auth_cache: AuthCache,
        grace_period_seconds: float = 14400,  # 4 hours
    ) -> None:
        self._auth_cache = auth_cache
        self._grace_period = grace_period_seconds
        self._offline_since: float | None = None
        self._needs_refresh = False

    @property
    def is_offline(self) -> bool:
        """Whether the manager is in offline mode."""
        return self._offline_since is not None

    @property
    def needs_refresh(self) -> bool:
        """Whether a forced token refresh is pending."""
        return self._needs_refresh

    def enter_offline_mode(self) -> None:
        """Mark the start of offline mode (extends auth grace period)."""
        if self._offline_since is None:
            self._offline_since = time.monotonic()
            logger.info(
                "Auth cache entering offline mode (grace period: %.0fs)", self._grace_period
            )

    def exit_offline_mode(self) -> None:
        """Exit offline mode and flag that auth refresh is needed."""
        if self._offline_since is not None:
            elapsed = time.monotonic() - self._offline_since
            self._offline_since = None
            self._needs_refresh = True
            logger.info("Auth cache exiting offline mode (was offline %.1fs)", elapsed)

    def is_grace_period_valid(self) -> bool:
        """Check if cached tokens are still within the grace period."""
        if self._offline_since is None:
            return True
        elapsed = time.monotonic() - self._offline_since
        return elapsed < self._grace_period

    def get_cached_auth(self, token: str) -> dict[str, Any] | None:
        """Get cached auth result, respecting offline grace period."""
        if self.is_offline and not self.is_grace_period_valid():
            logger.warning("Auth grace period expired — cached token no longer valid")
            return None
        return self._auth_cache.get(token)

    async def force_refresh(self) -> None:
        """Invalidate all cached tokens so they are re-authenticated on next use.

        Must be called during reconnection before any data operations.
        """
        self._auth_cache.clear()
        self._needs_refresh = False
        logger.info("Forced auth refresh — cache cleared for reconnection")

    def clear(self) -> None:
        """Clear all cached auth state."""
        self._auth_cache.clear()
        self._offline_since = None
        self._needs_refresh = False
