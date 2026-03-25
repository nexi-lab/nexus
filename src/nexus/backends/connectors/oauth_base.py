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

    # Maps generic provider names to oauth.yaml config names.
    # All Google services share the same OAuth client credentials, so a user
    # who passes provider="google" should resolve to the connector-specific
    # config entry (gmail, gcalendar, google-drive, etc.).
    _PROVIDER_ALIASES: dict[str, list[str]] = {
        "google": ["gmail", "gcalendar", "google-drive", "google-cloud-storage"],
    }

    def _register_oauth_provider(self) -> None:
        """Register OAuth provider with TokenManager using OAuthProviderFactory.

        Tries the configured provider name first, then falls back to the
        backend's canonical name, then generic aliases. This handles the common
        case where a user mounts with provider="google" but oauth.yaml defines
        "gmail" or "gcalendar".
        """
        try:
            import importlib as _il

            OAuthProviderFactory = _il.import_module(
                "nexus.bricks.auth.oauth.factory"
            ).OAuthProviderFactory

            factory = OAuthProviderFactory()

            # Build candidate list: configured name → backend name → aliases
            candidates = [self.provider]
            backend_name = getattr(self, "name", "")
            if backend_name and backend_name != self.provider:
                candidates.append(backend_name)
            for alias, targets in self._PROVIDER_ALIASES.items():
                if self.provider == alias:
                    candidates.extend(targets)

            for candidate in candidates:
                try:
                    provider_instance = factory.create_provider(name=candidate)
                    self.token_manager.register_provider(self.provider, provider_instance)
                    logger.info(
                        "Registered OAuth provider '%s' (resolved from '%s') for %s backend",
                        candidate,
                        self.provider,
                        self.name,
                    )
                    return
                except ValueError:
                    continue

            logger.warning(
                "OAuth provider '%s' not available (tried: %s). "
                "OAuth flow must be initiated manually via the Integrations page.",
                self.provider,
                ", ".join(candidates),
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
