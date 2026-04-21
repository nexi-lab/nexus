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
    ReadmeDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.oauth import OAuthConnectorMixin
from nexus.contracts.backend_features import OAUTH_BACKEND_FEATURES
from nexus.contracts.exceptions import AuthenticationError

if TYPE_CHECKING:
    from nexus.bricks.auth.credential_pool import CredentialPool
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connector-agnostic OAuth auth-required helper (Issue #3822 / PR #3825)
# ---------------------------------------------------------------------------


def build_auth_recovery_hint(
    *,
    connector_name: str,
    provider: str,
    user_email: str | None = None,
) -> dict[str, str]:
    """Return a machine-actionable re-auth pointer for the connector auth API.

    Matches the real ``AuthInitRequest`` contract on
    ``POST /api/v2/connectors/auth/init`` — emitted verbatim on
    :class:`AuthenticationError.recovery_hint` so clients can drive
    recovery without out-of-band guesswork.  ``connector_name`` must be
    the registry-registered name (e.g. ``gmail_connector``,
    ``gdrive_connector``), not the provider label.
    """
    hint: dict[str, str] = {
        "endpoint": "/api/v2/connectors/auth/init",
        "method": "POST",
        "connector_name": connector_name,
        "provider": provider,
    }
    if user_email:
        hint["user_email"] = user_email
    return hint


def _looks_like_email(value: str | None) -> bool:
    return bool(value and "@" in value)


def _resolve_linked_oauth_email(
    token_manager: Any,
    *,
    provider: str,
    nexus_user_id: str,
    zone_id: str,
) -> str | None:
    """Look up the OAuth-linked email for a nexus user on *provider*.

    OAuth credentials are stored keyed by ``(provider, user_email, zone_id)``
    with the nexus ``user_id`` recorded as a secondary column.  For
    API-key-authenticated requests the transport's ``context.user_id`` is
    the nexus user id (e.g. ``"admin"``), not the gmail address Google
    stored against the credential — so ``get_valid_token(user_email="admin")``
    404s.  This helper bridges the gap: find any non-revoked credential
    whose ``user_id`` matches and return its ``user_email``.  Returns
    ``None`` if no link exists.  Kept synchronous so the transports can
    call it without an async hop.
    """
    from nexus.lib.sync_bridge import run_sync

    list_fn = getattr(token_manager, "list_credentials", None)
    if list_fn is None:
        return None
    try:
        creds = run_sync(list_fn(zone_id=zone_id, user_id=nexus_user_id))
    except Exception:
        return None
    for cred in creds or []:
        if cred.get("provider") == provider and _looks_like_email(cred.get("user_email")):
            return str(cred["user_email"])
    return None


def resolve_oauth_access_token(
    token_manager: Any,
    *,
    connector_name: str,
    provider: str,
    user_email: str | None,
    zone_id: str = "root",
    nexus_user_id: str | None = None,
) -> str:
    """Resolve an OAuth access token or raise a structured ``AuthenticationError``.

    Connector-agnostic replacement for each transport's inline
    ``try: get_valid_token(...); except Exception: raise BackendError(...)``
    pattern.  Resolution order:

    1. ``user_email`` if it looks like an email — use it verbatim.
    2. ``user_email`` that is actually a nexus user id (no ``@``) or
       ``None`` — consult the token manager's credential index for the
       nexus user's link to *provider* and substitute the stored OAuth
       email.  This is the one place callers can rely on for the
       cross-mapping, so every transport gets the same behaviour
       without duplicating the lookup.
    3. Still nothing — raise ``AuthenticationError`` with
       ``recovery_hint`` so clients see 401 + actionable payload.

    Auth-required conditions — missing ``user_email`` or a bubbling
    ``AuthenticationError`` from the token manager — are re-raised with
    ``provider``/``user_email``/``recovery_hint`` so the server error
    handler maps them to HTTP 401 with actionable payload (Issue #3822).
    Non-auth failures (network, misconfig) are *not* caught here so
    callers can raise them as ``BackendError`` without the silent
    BackendError-masking-AuthenticationError problem earlier connectors
    had.
    """
    from nexus.lib.sync_bridge import run_sync

    # When caller passes a nexus user id in user_email (common API-key
    # path where context.user_id is a subject id like "admin"), swap in
    # the linked OAuth email.  user_email=None similarly falls back to
    # the nexus_user_id hint if one was provided by the caller.
    resolved_email = user_email if _looks_like_email(user_email) else None
    candidate_user_id = nexus_user_id or (None if _looks_like_email(user_email) else user_email)
    if resolved_email is None and candidate_user_id:
        resolved_email = _resolve_linked_oauth_email(
            token_manager,
            provider=provider,
            nexus_user_id=candidate_user_id,
            zone_id=zone_id,
        )

    if resolved_email is None:
        raise AuthenticationError(
            f"{connector_name} requires authorization (provider={provider}). "
            "Configure user_email on the mount or complete the OAuth flow "
            "for the authenticated nexus user.",
            provider=provider,
            user_email=user_email,
            recovery_hint=build_auth_recovery_hint(
                connector_name=connector_name,
                provider=provider,
                user_email=user_email if _looks_like_email(user_email) else None,
            ),
        )
    try:
        access_token: str = run_sync(
            token_manager.get_valid_token(
                provider=provider,
                user_email=resolved_email,
                zone_id=zone_id,
            )
        )
        return access_token
    except AuthenticationError as _auth_exc:
        raise AuthenticationError(
            str(_auth_exc),
            provider=provider,
            user_email=resolved_email,
            recovery_hint=build_auth_recovery_hint(
                connector_name=connector_name,
                provider=provider,
                user_email=resolved_email,
            ),
        ) from _auth_exc


class OAuthConnectorBase(
    Backend,
    OAuthConnectorMixin,
    ReadmeDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
    CheckpointMixin,
):
    """Shared base class for OAuth API-backed connectors.

    Provides:
    - Common capability set (OAUTH)
    - OAuth initialization and token management
    - OAuth provider registration via factory
    - Cache session factory setup
    - Checkpoint mixin state initialization

    Subclasses must implement:
    - ``name`` property
    - ``read_content()``
    - ``list_dir()``
    - Connector-specific ``SKILL_NAME``, ``SCHEMAS``, ``OPERATION_TRAITS``
    """

    _BACKEND_FEATURES = OAUTH_BACKEND_FEATURES

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "oauth",
        record_store: "RecordStoreABC | None" = None,
        metadata_store: Any = None,
        encryption_key: str | None = None,
        pool: "CredentialPool | None" = None,
    ) -> None:
        """Initialize OAuth connector base.

        Args:
            token_manager_db: Path to TokenManager database or database URL.
            user_email: Optional user email for OAuth lookup. None = use context.
            provider: OAuth provider name from config.
            record_store: Optional RecordStoreABC instance for content caching.
            metadata_store: MetastoreABC instance for file_paths table (optional).
            pool: Optional CredentialPool for multi-account failover (Issue #3723).
                When set, the connector selects an account from the pool per-request
                and rotates on rate-limit / quota errors. When None (default),
                falls back to single-account behaviour (no change for existing users).
        """
        super().__init__()
        self._init_oauth(
            token_manager_db,
            user_email=user_email,
            provider=provider,
            encryption_key=encryption_key,
        )
        self.session_factory = record_store.session_factory if record_store else None
        self.metadata_store = metadata_store

        # Initialize CheckpointMixin state (MRO doesn't call CheckpointMixin.__init__)
        self._checkpoints: dict[str, Any] = {}

        # Credential pool — None means single-account behaviour (default)
        self._pool: CredentialPool | None = pool

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
