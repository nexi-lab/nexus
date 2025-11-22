"""OAuth operations for NexusFS.

This module contains OAuth credential management operations:
- oauth_get_auth_url: Get OAuth authorization URL for provider
- oauth_exchange_code: Exchange authorization code for tokens
- oauth_list_credentials: List all stored OAuth credentials
- oauth_revoke_credential: Revoke OAuth credential
- oauth_test_credential: Test credential validity
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.server.auth.token_manager import TokenManager

logger = logging.getLogger(__name__)


class NexusFSOAuthMixin:
    """Mixin providing OAuth credential management operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        db_path: str | None
        _token_manager: TokenManager | None

    def _get_token_manager(self) -> TokenManager:
        """Get or create TokenManager instance.

        Returns:
            TokenManager instance

        Raises:
            RuntimeError: If TokenManager cannot be initialized
        """
        if not hasattr(self, "_token_manager") or self._token_manager is None:
            from nexus.server.auth.token_manager import TokenManager

            # Use the same database as NexusFS
            db_path = str(self.db_path) if hasattr(self, "db_path") and self.db_path else None
            if not db_path:
                raise RuntimeError("Cannot initialize TokenManager: no database path configured")

            # Check if db_path is a database URL (postgresql://, mysql://, etc.) or a file path
            if db_path.startswith(("postgresql://", "mysql://", "sqlite://")):
                # It's already a database URL
                self._token_manager = TokenManager(db_url=db_path)
            else:
                # It's a file path for SQLite
                self._token_manager = TokenManager(db_path=db_path)

        return self._token_manager

    @rpc_expose(description="Get OAuth authorization URL for Google Drive")
    def oauth_get_drive_auth_url(
        self,
        redirect_uri: str = "http://localhost:3000/oauth/callback",
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Get OAuth authorization URL for Google Drive.

        Args:
            redirect_uri: OAuth redirect URI (default: http://localhost:3000/oauth/callback)
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - url: Authorization URL for user to visit
                - state: CSRF state token (should be verified in callback)

        Raises:
            RuntimeError: If OAuth credentials not configured
        """
        from nexus.server.auth.google_oauth import GoogleOAuthProvider

        # Get client ID and secret from environment
        client_id = os.environ.get("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")

        if not client_id or not client_secret:
            raise RuntimeError(
                "Google OAuth credentials not configured. "
                "Set NEXUS_OAUTH_GOOGLE_CLIENT_ID and NEXUS_OAUTH_GOOGLE_CLIENT_SECRET "
                "environment variables."
            )

        # Create provider
        provider = GoogleOAuthProvider(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.file",
            ],
        )

        # Register provider with TokenManager for later use
        token_manager = self._get_token_manager()
        token_manager.register_provider("google", provider)

        # Generate authorization URL with state token
        import secrets

        state = secrets.token_urlsafe(32)
        auth_url = provider.get_authorization_url(state=state)

        logger.info(f"Generated OAuth authorization URL for Google Drive (state={state})")

        return {
            "url": auth_url,
            "state": state,
        }

    @rpc_expose(description="Exchange OAuth authorization code for tokens")
    async def oauth_exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str,
        state: str | None = None,  # noqa: ARG002
        redirect_uri: str = "http://localhost:3000/oauth/callback",
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Exchange OAuth authorization code for tokens and store credentials.

        Args:
            provider: OAuth provider name (e.g., "google")
            code: Authorization code from OAuth callback
            user_email: User email address for credential storage
            state: CSRF state token (optional, for validation)
            redirect_uri: OAuth redirect URI (must match authorization request)
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - credential_id: Unique credential identifier
                - user_email: User email
                - expires_at: Token expiration timestamp (ISO format)
                - success: True if successful

        Raises:
            RuntimeError: If OAuth credentials not configured
            ValueError: If code exchange fails
        """
        from nexus.server.auth.google_oauth import GoogleOAuthProvider

        logger.info(f"Exchanging OAuth code for provider={provider}, user={user_email}")

        # Get client credentials from environment
        if provider == "google":
            client_id = os.environ.get("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
            client_secret = os.environ.get("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")

            if not client_id or not client_secret:
                raise RuntimeError(
                    "Google OAuth credentials not configured. "
                    "Set NEXUS_OAUTH_GOOGLE_CLIENT_ID and NEXUS_OAUTH_GOOGLE_CLIENT_SECRET."
                )

            # Create provider
            provider_instance = GoogleOAuthProvider(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scopes=[
                    "https://www.googleapis.com/auth/drive",
                    "https://www.googleapis.com/auth/drive.file",
                ],
            )
        else:
            raise ValueError(f"Unsupported OAuth provider: {provider}")

        # Get TokenManager
        token_manager = self._get_token_manager()
        token_manager.register_provider(provider, provider_instance)

        # Exchange code for credential
        try:
            credential = await provider_instance.exchange_code(code)
        except Exception as e:
            logger.error(f"Failed to exchange OAuth code: {e}")
            raise ValueError(f"Failed to exchange authorization code: {e}") from e

        # Store credential
        # Default to 'default' tenant if not specified to match mount configurations
        tenant_id = (
            context.tenant_id if context and hasattr(context, "tenant_id") else None
        ) or "default"
        created_by = context.user_id if context and hasattr(context, "user_id") else user_email

        try:
            credential_id = await token_manager.store_credential(
                provider=provider,
                user_email=user_email,
                credential=credential,
                tenant_id=tenant_id,
                created_by=created_by,
            )

            logger.info(
                f"Successfully stored OAuth credential for {user_email} "
                f"(credential_id={credential_id})"
            )

            return {
                "credential_id": credential_id,
                "user_email": user_email,
                "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
                "success": True,
            }
        except Exception as e:
            logger.error(f"Failed to store OAuth credential: {e}")
            raise ValueError(f"Failed to store credential: {e}") from e

    @rpc_expose(description="List all OAuth credentials")
    async def oauth_list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List all OAuth credentials for the current user.

        Args:
            provider: Optional provider filter (e.g., "google")
            include_revoked: Include revoked credentials (default: False)
            context: Operation context (optional)

        Returns:
            List of credential dictionaries containing:
                - credential_id: Unique identifier
                - provider: OAuth provider name
                - user_email: User email
                - scopes: List of granted scopes
                - expires_at: Token expiration timestamp (ISO format)
                - created_at: Creation timestamp (ISO format)
                - last_used_at: Last usage timestamp (ISO format)
                - revoked: Whether credential is revoked
        """
        token_manager = self._get_token_manager()
        # Default to 'default' tenant if not specified to match mount configurations
        tenant_id = (
            context.tenant_id if context and hasattr(context, "tenant_id") else None
        ) or "default"

        credentials = await token_manager.list_credentials(tenant_id=tenant_id)

        # Filter by provider and revoked status if needed
        result = []
        for cred in credentials:
            if provider and cred["provider"] != provider:
                continue
            if not include_revoked and cred.get("revoked", False):
                continue
            result.append(cred)

        logger.info(f"Listed {len(result)} OAuth credentials (provider={provider})")
        return result

    @rpc_expose(description="Revoke OAuth credential")
    async def oauth_revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Revoke an OAuth credential.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - success: True if revoked successfully
                - credential_id: Revoked credential ID

        Raises:
            ValueError: If credential not found
        """
        token_manager = self._get_token_manager()
        # Default to 'default' tenant if not specified to match mount configurations
        tenant_id = (
            context.tenant_id if context and hasattr(context, "tenant_id") else None
        ) or "default"

        try:
            success = await token_manager.revoke_credential(
                provider=provider,
                user_email=user_email,
                tenant_id=tenant_id,
            )

            if success:
                logger.info(f"Revoked OAuth credential for {provider}:{user_email}")
                return {"success": True}
            else:
                raise ValueError(f"Credential not found: {provider}:{user_email}")

        except Exception as e:
            logger.error(f"Failed to revoke credential: {e}")
            raise ValueError(f"Failed to revoke credential: {e}") from e

    @rpc_expose(description="Test OAuth credential validity")
    async def oauth_test_credential(
        self,
        provider: str,
        user_email: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Test if an OAuth credential is valid and can be refreshed.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - valid: True if credential is valid
                - refreshed: True if token was refreshed
                - expires_at: Token expiration timestamp (ISO format)
                - error: Error message if invalid

        Raises:
            ValueError: If credential not found
        """
        token_manager = self._get_token_manager()
        # Default to 'default' tenant if not specified to match mount configurations
        tenant_id = (
            context.tenant_id if context and hasattr(context, "tenant_id") else None
        ) or "default"

        try:
            # Try to get a valid token (will auto-refresh if needed)
            token = await token_manager.get_valid_token(
                provider=provider,
                user_email=user_email,
                tenant_id=tenant_id,
            )

            if token:
                # Get credential details
                credentials = await token_manager.list_credentials(tenant_id=tenant_id)
                cred = next((c for c in credentials if c["user_email"] == user_email), None)

                logger.info(f"OAuth credential test successful for {provider}:{user_email}")
                return {
                    "valid": True,
                    "refreshed": True,  # If we got here, token was valid or refreshed
                    "expires_at": cred["expires_at"] if cred and cred.get("expires_at") else None,
                }
            else:
                return {
                    "valid": False,
                    "error": "Could not retrieve valid token",
                }

        except Exception as e:
            logger.error(f"OAuth credential test failed: {e}")
            return {
                "valid": False,
                "error": str(e),
            }
