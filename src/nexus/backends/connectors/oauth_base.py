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
    connector_name: str | None = None,
) -> str | None:
    """Look up the unambiguous OAuth-linked email for a nexus user on *provider*.

    OAuth credentials are stored keyed by ``(provider, user_email, zone_id)``
    with the nexus ``user_id`` recorded as a secondary column.  For
    API-key-authenticated requests the transport's ``context.user_id`` is
    the nexus user id (e.g. ``"admin"``), not the gmail address Google
    stored against the credential — so ``get_valid_token(user_email="admin")``
    404s.  This helper bridges the gap: find the credential whose
    ``user_id`` matches and return its ``user_email``.

    Safety contract:
    * Returns ``None`` when no match exists (auth-required).
    * Raises ``AuthenticationError`` when the same nexus user has
      *more than one* active credential for the provider.  Silently
      picking the first row would be a cross-account leak vector.
    * Does **not** catch lookup exceptions — DB/network/session errors
      propagate so callers can surface them as backend failures instead
      of false 401s.

    Kept synchronous so the transports can call it without an async hop.
    """
    from nexus.lib.sync_bridge import run_sync

    list_fn = getattr(token_manager, "list_credentials", None)
    if list_fn is None:
        return None
    # NB: intentionally do not wrap in try/except — credential-index
    # failures (DB down, session closed, encryption error) must surface
    # as 5xx backend errors, not be downgraded to "auth required" which
    # would push the client into a re-auth loop that cannot succeed.
    creds = run_sync(list_fn(zone_id=zone_id, user_id=nexus_user_id))
    matches: list[str] = []
    malformed_count = 0
    for cred in creds or []:
        if cred.get("provider") != provider:
            continue
        email = cred.get("user_email")
        email = email.strip() if isinstance(email, str) else email
        if _looks_like_email(email):
            matches.append(str(email))
        else:
            # A credential row exists for this nexus user + provider but
            # the stored ``user_email`` is blank or malformed — that's a
            # data-quality problem, not a missing-credential problem.
            # Silently dropping the row would generate a non-actionable
            # 401 loop; surface it with a specific message so operators
            # can investigate / re-link.
            malformed_count += 1
    if not matches:
        if malformed_count:
            # Preserve the standard auth-init recovery contract
            # (endpoint / method / connector_name / provider) that
            # clients already key on, then extend it with the
            # malformed-row-specific fields so clients that know the
            # extension can surface a relink prompt.  ``connector_name``
            # is threaded in by callers (``resolve_oauth_access_token``
            # passes its own ``connector_name``) so the payload maps
            # cleanly onto ``AuthInitRequest``.
            hint: dict[str, str | list[str]] = {
                "endpoint": "/api/v2/connectors/auth/init",
                "method": "POST",
                "provider": provider,
                "action": "relink_credential",
                "user_id": nexus_user_id,
            }
            if connector_name:
                hint["connector_name"] = connector_name
            raise AuthenticationError(
                f"Found {malformed_count} OAuth credential row(s) for nexus "
                f"user {nexus_user_id!r} + provider {provider!r} but each "
                "one has a blank or malformed user_email.  Re-run the OAuth "
                "flow to refresh the credential, or pin the mount to the "
                "correct email explicitly.",
                provider=provider,
                user_email=None,
                recovery_hint=hint,
            )
        return None
    unique = sorted(set(matches))
    if len(unique) > 1:
        hint_ambiguous: dict[str, str | list[str]] = {
            "endpoint": "/api/v2/connectors/auth/init",
            "method": "POST",
            "provider": provider,
            "action": "select_account",
            "candidates": unique,
        }
        if connector_name:
            hint_ambiguous["connector_name"] = connector_name
        raise AuthenticationError(
            f"Multiple OAuth accounts linked to nexus user {nexus_user_id!r} "
            f"for provider {provider!r}: {unique}. Pin the mount to an "
            "explicit user_email to disambiguate.",
            provider=provider,
            user_email=None,
            recovery_hint=hint_ambiguous,
        )
    return unique[0]


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

    # Explicit mount identity is authoritative.  Priority:
    #   1. ``user_email`` set on the mount — whether it looks like an email
    #      (use verbatim) or a nexus subject id (look it up via the
    #      credential index).  The mount owner pinned this identity and
    #      must not be silently overridden by whoever happens to be
    #      calling — that would be a cross-account leak vector.
    #   2. Only when ``user_email`` is absent, fall back to the
    #      ``nexus_user_id`` from the request context.  An email-shaped
    #      context id flows straight through; a subject id goes through
    #      the credential-index lookup.
    # This shape preserves the unchanged legacy behaviour (mount pin wins)
    # while still fixing the false-401 case where only the context has an
    # identity to offer.
    #
    # Normalize blank / whitespace-only strings to ``None`` up front:
    # mount configs sometimes render ``user_email: ""`` for "no pin",
    # and treating that as an authoritative empty pin would lock every
    # request out with a false 401.
    def _norm(v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None

    user_email = _norm(user_email)
    nexus_user_id = _norm(nexus_user_id)

    resolved_email: str | None = None
    candidate_user_id: str | None = None

    if user_email is not None:
        # Mount pin is authoritative — do not fall through to the request
        # context even if lookup misses.
        if _looks_like_email(user_email):
            resolved_email = user_email
        else:
            candidate_user_id = user_email
    elif nexus_user_id is not None:
        if _looks_like_email(nexus_user_id):
            resolved_email = nexus_user_id
        else:
            candidate_user_id = nexus_user_id

    if resolved_email is None and candidate_user_id:
        resolved_email = _resolve_linked_oauth_email(
            token_manager,
            provider=provider,
            nexus_user_id=candidate_user_id,
            zone_id=zone_id,
            connector_name=connector_name,
        )

    if resolved_email is None:
        # Distinguish the two auth-required sub-cases so operators can
        # tell a mount-config mistake from a missing-credential one:
        #   * mount pinned a non-email subject with no matching
        #     credential row → point at the mount config,
        #   * nothing was pinned and context has nothing to offer →
        #     point at the OAuth flow.
        if user_email is not None and not _looks_like_email(user_email):
            msg = (
                f"{connector_name} mount is pinned to user_email={user_email!r} "
                f"(provider={provider}), but no OAuth credential is linked to "
                "that nexus user.  Either repin the mount to the authorized "
                "email, or complete the OAuth flow for this subject."
            )
        else:
            msg = (
                f"{connector_name} requires authorization (provider={provider}). "
                "Configure user_email on the mount or complete the OAuth flow "
                "for the authenticated nexus user."
            )
        raise AuthenticationError(
            msg,
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
