"""Shared OAuth setup mixin for connector backends (Issue #1601).

Extracts the duplicated TokenManager initialization boilerplate from
Gmail, Google Calendar, Slack, and X connectors into a single mixin.

Usage:
    class MyOAuthConnector(Backend, OAuthConnectorMixin):
        def __init__(self, token_manager_db, user_email=None, provider="my_provider", ...):
            self._init_oauth(token_manager_db, user_email=user_email, provider=provider)
            ...
"""

import logging

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
        encryption_key: str | None = None,
    ) -> None:
        """Initialize OAuth token management.

        Sets ``self.token_manager_db``, ``self.user_email``,
        ``self.provider``, and ``self.token_manager``.

        Args:
            token_manager_db: Path to TokenManager database or database URL
            user_email: Optional user email for OAuth lookup. None = use context.
            provider: OAuth provider name from config
            encryption_key: Fernet encryption key for token storage. Must match
                the key used by exchange_auth_code() in the same session so stored
                tokens are readable. In slim-fs mode this is always provided by
                _backend_factory via get_oauth_encryption_key().
        """
        import importlib as _il

        TokenManager = _il.import_module("nexus.bricks.auth.oauth.token_manager").TokenManager

        self.token_manager_db = token_manager_db
        self.user_email = user_email
        self.provider = provider

        from nexus.backends.connectors.utils import resolve_database_url

        resolved_db = resolve_database_url(token_manager_db)
        extra = {"encryption_key": encryption_key} if encryption_key else {}
        if resolved_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=resolved_db, **extra)
        else:
            self.token_manager = TokenManager(db_path=resolved_db, **extra)
