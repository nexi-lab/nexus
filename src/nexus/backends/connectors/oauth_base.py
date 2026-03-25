"""OAuthConnectorBase — shared base class for OAuth API-backed connectors (Issue #3266).

Consolidates the duplicated inheritance chain, capabilities, OAuth init, token
management, and provider registration from Gmail and Calendar connectors.

Concrete connectors inherit from this and add only their specific logic:
schemas, traits, read/write methods, API service builders.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.backends.base.backend import Backend
from nexus.backends.connectors.base import (
    CheckpointMixin,
    SkillDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.oauth import OAuthConnectorMixin
from nexus.backends.wrappers.cache_mixin import CacheConnectorMixin
from nexus.contracts.capabilities import OAUTH_CONNECTOR_CAPABILITIES, ConnectorCapability

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class OAuthConnectorBase(
    Backend,
    CacheConnectorMixin,
    OAuthConnectorMixin,
    SkillDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
    CheckpointMixin,
):
    """Shared base class for OAuth API-backed connectors.

    Provides:
    - Common capability set (OAUTH + CACHE_BULK_READ + CACHE_SYNC + SYNC_ELIGIBLE)
    - OAuth initialization and token management
    - OAuth provider registration via factory
    - Metadata store integration for metastore-first listing
    - Cache session factory setup
    - Checkpoint mixin state initialization

    Subclasses must implement:
    - ``name`` property
    - ``read_content()``
    - ``list_dir()``
    - Connector-specific ``SKILL_NAME``, ``SCHEMAS``, ``OPERATION_TRAITS``
    """

    _CAPABILITIES = OAUTH_CONNECTOR_CAPABILITIES | frozenset(
        {
            ConnectorCapability.CACHE_BULK_READ,
            ConnectorCapability.CACHE_SYNC,
        }
    )

    # Enable metadata-based listing (use file_paths table for fast queries)
    use_metadata_listing = True

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "oauth",
        record_store: "RecordStoreABC | None" = None,
        metadata_store: Any = None,
    ) -> None:
        """Initialize OAuth connector base.

        Args:
            token_manager_db: Path to TokenManager database or database URL.
            user_email: Optional user email for OAuth lookup. None = use context.
            provider: OAuth provider name from config.
            record_store: Optional RecordStoreABC instance for content caching.
            metadata_store: MetastoreABC instance for file_paths table (optional).
        """
        super().__init__()
        self._init_oauth(token_manager_db, user_email=user_email, provider=provider)
        self.session_factory = record_store.session_factory if record_store else None
        self.metadata_store = metadata_store

        # Initialize CheckpointMixin state (MRO doesn't call CheckpointMixin.__init__)
        self._checkpoints: dict[str, Any] = {}

        # Register OAuth provider using factory (loads from config)
        self._register_oauth_provider()

    def _register_oauth_provider(self) -> None:
        """Register OAuth provider with TokenManager using OAuthProviderFactory."""
        try:
            import importlib as _il

            OAuthProviderFactory = _il.import_module(
                "nexus.bricks.auth.oauth.factory"
            ).OAuthProviderFactory

            factory = OAuthProviderFactory()

            try:
                provider_instance = factory.create_provider(name=self.provider)
                self.token_manager.register_provider(self.provider, provider_instance)
                logger.info(
                    "Registered OAuth provider '%s' for %s backend", self.provider, self.name
                )
            except ValueError as e:
                logger.warning(
                    "OAuth provider '%s' not available: %s. "
                    "OAuth flow must be initiated manually via the Integrations page.",
                    self.provider,
                    e,
                )
        except Exception as e:
            logger.error("Failed to register OAuth provider: %s", e)

    @property
    def user_scoped(self) -> bool:
        """OAuth connectors require per-user credentials."""
        return True

    @property
    def has_token_manager(self) -> bool:
        """OAuth connectors manage tokens."""
        return True
