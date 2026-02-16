"""Shared OAuth setup mixin for connector backends (Issue #1601).

Extracts the duplicated TokenManager initialization boilerplate from
Gmail, Google Calendar, Slack, and X connectors into a single mixin.

Usage:
    class MyOAuthConnector(Backend, OAuthConnectorMixin, CacheConnectorMixin):
        def __init__(self, token_manager_db, user_email=None, provider="my_provider", ...):
            self._init_oauth(token_manager_db, user_email=user_email, provider=provider)
            ...
"""

from __future__ import annotations

import logging

from nexus.backends.backend import Backend

logger = logging.getLogger(__name__)


class OAuthConnectorMixin:
    """Shared OAuth setup for connector backends.

    Handles TokenManager initialization, database URL resolution,
    and common OAuth attributes (token_manager_db, user_email, provider).
    """

    def _init_oauth(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "oauth",
    ) -> None:
        """Initialize OAuth token management.

        Sets ``self.token_manager_db``, ``self.user_email``,
        ``self.provider``, and ``self.token_manager``.

        Args:
            token_manager_db: Path to TokenManager database or database URL
            user_email: Optional user email for OAuth lookup. None = use context.
            provider: OAuth provider name from config
        """
        from nexus.server.auth.token_manager import TokenManager

        self.token_manager_db = token_manager_db
        self.user_email = user_email
        self.provider = provider

        resolved_db = Backend.resolve_database_url(token_manager_db)
        if resolved_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=resolved_db)
        else:
            self.token_manager = TokenManager(db_path=resolved_db)
