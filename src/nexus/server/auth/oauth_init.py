"""OAuth provider initialization.

Extracted from ``nexus.server.fastapi_server._initialize_oauth_provider`` (#2049)
to reduce the size of the monolithic server module.
"""

import logging
import os
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import DEFAULT_GOOGLE_REDIRECT_URI

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


def initialize_oauth_provider(nexus_fs: "NexusFS", auth_provider: Any) -> None:
    """Initialize OAuth provider if Google OAuth credentials are available.

    Args:
        nexus_fs: NexusFS instance
        auth_provider: Authentication provider (for session factory)
    """
    try:
        google_client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv(
            "NEXUS_OAUTH_GOOGLE_CLIENT_ID"
        )
        google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv(
            "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET"
        )
        google_redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", DEFAULT_GOOGLE_REDIRECT_URI)
        jwt_secret = os.getenv("NEXUS_JWT_SECRET")

        if not google_client_id or not google_client_secret:
            logger.debug(
                "Google OAuth credentials not found. OAuth endpoints will return 500 errors."
            )
            return

        # Get session factory from auth_provider or nexus_fs
        session_factory = None
        if auth_provider and hasattr(auth_provider, "session_factory"):
            session_factory = auth_provider.session_factory
        elif hasattr(nexus_fs, "SessionLocal") and nexus_fs.SessionLocal is not None:
            session_factory = nexus_fs.SessionLocal
        else:
            logger.warning("Cannot initialize OAuth provider: no session factory available")
            return

        if not session_factory:
            logger.warning("Cannot initialize OAuth provider: session factory is None")
            return

        # Initialize OAuth provider (provider-agnostic via DI)
        from nexus.bricks.auth.oauth.crypto import OAuthCrypto
        from nexus.bricks.auth.oauth.providers.google import GoogleOAuthProvider
        from nexus.bricks.auth.oauth.user_auth import OAuthUserAuth
        from nexus.server.auth.auth_routes import set_oauth_provider
        from nexus.server.auth.oauth_state_store import initialize_oauth_state_service

        _oauth_enc_key = os.environ.get("NEXUS_OAUTH_ENCRYPTION_KEY", "").strip() or None
        _settings_store = None
        try:
            from nexus.storage.auth_stores.metastore_settings_store import MetastoreSettingsStore

            _settings_store = MetastoreSettingsStore(nexus_fs.metadata)
        except Exception:
            pass
        oauth_crypto = OAuthCrypto(encryption_key=_oauth_enc_key, settings_store=_settings_store)

        # Build provider-agnostic providers dict
        google_provider = GoogleOAuthProvider(
            client_id=google_client_id,
            client_secret=google_client_secret,
            redirect_uri=google_redirect_uri,
            scopes=[
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            ],
            provider_name="google-auth",
        )
        # Issue #2281: inject UserProvisionerProtocol to break circular dep
        user_provisioner = None
        try:
            from nexus.bricks.auth.stores.nexusfs_provisioner import NexusFSUserProvisioner

            user_provisioner = NexusFSUserProvisioner(nexus_fs)
        except Exception:
            logger.debug("NexusFSUserProvisioner unavailable; OAuth user provisioning disabled")

        oauth_provider = OAuthUserAuth(
            session_factory=session_factory,
            providers={"google": google_provider},
            jwt_secret=jwt_secret,
            oauth_crypto=oauth_crypto,
            user_provisioner=user_provisioner,
        )

        set_oauth_provider(oauth_provider)

        # Initialize the stateless, signed OAuth state service. Shares the
        # JWT secret because (a) it's already a server-held secret and
        # (b) callers that hold the secret already hold the power to issue
        # tokens. Stateless verification means this works across any
        # number of uvicorn workers / replicas without sticky sessions.
        state_signing_secret = jwt_secret or oauth_provider.local_auth.jwt_secret
        if not state_signing_secret:
            raise RuntimeError(
                "Cannot initialize OAuth state service: no JWT secret available. "
                "Set NEXUS_JWT_SECRET or configure an auth provider with a jwt_secret."
            )
        initialize_oauth_state_service(state_signing_secret)
        logger.info("Google OAuth provider initialized successfully")
    except Exception as e:
        logger.warning(
            f"Failed to initialize OAuth provider: {e}. OAuth endpoints will not be available."
        )

    # NexusFS instance is now registered unconditionally in create_app()
    # (moved from here to avoid being gated on OAuth credentials)
