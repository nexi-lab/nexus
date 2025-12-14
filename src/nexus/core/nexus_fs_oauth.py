"""OAuth operations for NexusFS.

This module contains OAuth credential management operations using OAuthProviderFactory.
All provider creation is done through the factory - no legacy fallback code.

Key features:
1. Provider name mapping to config names
2. Unified provider creation through factory
3. PKCE support for providers that require it
4. Simplified error handling flow
5. No duplicate code

Operations:
- oauth_list_providers: List all available OAuth providers
- oauth_get_auth_url: Get OAuth authorization URL for any provider
- oauth_exchange_code: Exchange authorization code for tokens
- oauth_list_credentials: List all stored OAuth credentials
- oauth_revoke_credential: Revoke OAuth credential
- oauth_test_credential: Test credential validity
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any

from nexus.core.context_utils import get_database_url, get_tenant_id
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.server.auth.token_manager import TokenManager

logger = logging.getLogger(__name__)

# In-memory cache for PKCE data (state -> pkce_data)
# This is temporary storage for PKCE verifiers during OAuth flow
_pkce_cache: dict[str, dict[str, str]] = {}


class NexusFSOAuthMixin:
    """Mixin providing OAuth credential management operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        db_path: str | None
        _token_manager: TokenManager | None
        _oauth_factory: Any | None

    def _get_oauth_factory(self) -> Any:
        """Get or create OAuth provider factory."""
        if not hasattr(self, "_oauth_factory") or self._oauth_factory is None:
            from nexus.server.auth.oauth_factory import OAuthProviderFactory

            oauth_config = None
            if hasattr(self, "_config"):
                config = getattr(self, "_config", None)
                if config and hasattr(config, "oauth") and config.oauth:
                    oauth_config = config.oauth

            self._oauth_factory = OAuthProviderFactory(config=oauth_config)

        return self._oauth_factory

    def _get_token_manager(self) -> TokenManager:
        """Get or create TokenManager instance."""
        if not hasattr(self, "_token_manager") or self._token_manager is None:
            from nexus.server.auth.token_manager import TokenManager

            # Use centralized database URL resolution
            db_path = get_database_url(self)

            # TokenManager accepts db_url for any database type, or db_path for SQLite
            if db_path.startswith(("postgresql://", "mysql://", "sqlite://")):
                self._token_manager = TokenManager(db_url=db_path)
            else:
                self._token_manager = TokenManager(db_path=db_path)

        return self._token_manager

    def _map_provider_name(self, provider: str) -> str:
        """Map user-facing provider name to config provider name.

        Args:
            provider: User-facing provider name (e.g., "google", "microsoft")

        Returns:
            Config provider name (e.g., "google-drive", "microsoft-onedrive")
        """
        provider_name_map = {
            "google": "google-drive",  # Default to drive for user convenience
            "twitter": "x",
            "x": "x",
            "microsoft": "microsoft-onedrive",
            "microsoft-onedrive": "microsoft-onedrive",
        }
        return provider_name_map.get(provider, provider)

    def _create_provider(
        self,
        provider: str,
        redirect_uri: str | None = None,
        scopes: list[str] | None = None,
    ) -> Any:
        """Create OAuth provider instance using factory.

        Args:
            provider: User-facing provider name
            redirect_uri: OAuth redirect URI (optional, uses config default if not provided)
            scopes: Optional scopes (uses config defaults if not provided)

        Returns:
            OAuthProvider instance

        Raises:
            RuntimeError: If provider cannot be created
            ValueError: If provider not found in config
        """
        factory = self._get_oauth_factory()
        config_name = self._map_provider_name(provider)

        provider_instance = factory.create_provider(
            name=config_name,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )

        logger.debug(f"Created provider {provider} using factory (config: {config_name})")
        return provider_instance

    def _register_provider(self, provider_instance: Any) -> None:
        """Register provider with TokenManager.

        Args:
            provider_instance: OAuthProvider instance to register
        """
        token_manager = self._get_token_manager()
        token_manager.register_provider(provider_instance.provider_name, provider_instance)

    def _get_authorization_url_with_pkce_support(
        self,
        provider_instance: Any,
        provider: str,
        state: str,
    ) -> dict[str, Any]:
        """Get authorization URL with PKCE support if needed.

        Args:
            provider_instance: OAuthProvider instance
            provider: Provider name (for logging)
            state: CSRF state token

        Returns:
            Dictionary with url, state, and optionally pkce_data
        """
        # Check if provider requires PKCE from config
        factory = self._get_oauth_factory()
        config_name = self._map_provider_name(provider)
        provider_config = factory.get_provider_config(config_name)
        requires_pkce = provider_config and provider_config.requires_pkce

        if requires_pkce:
            auth_url, pkce_data = provider_instance.get_authorization_url_with_pkce(state=state)
            _pkce_cache[state] = pkce_data
            logger.info(
                f"Generated OAuth authorization URL for {provider} with PKCE (state={state})"
            )
            return {
                "url": auth_url,
                "state": state,
                "pkce_data": pkce_data,
            }
        else:
            auth_url = provider_instance.get_authorization_url(state=state)
            logger.info(f"Generated OAuth authorization URL for {provider} (state={state})")
            return {
                "url": auth_url,
                "state": state,
            }

    def _get_pkce_verifier(
        self,
        provider: str,
        code_verifier: str | None,
        state: str | None,
    ) -> str:
        """Get PKCE verifier from parameter or cache.

        This method should only be called for providers that require PKCE.

        Args:
            provider: Provider name
            code_verifier: PKCE verifier from parameter
            state: State token to look up in cache

        Returns:
            PKCE verifier string

        Raises:
            ValueError: If PKCE verifier cannot be found
        """
        # If provided directly, use it
        if code_verifier:
            return code_verifier

        # Try to get from cache using state
        if state:
            pkce_data = _pkce_cache.get(state)
            if pkce_data:
                verifier = pkce_data.get("code_verifier")
                if verifier:
                    _pkce_cache.pop(state, None)  # Clean up cache
                    return verifier

        # PKCE verifier not found
        raise ValueError(
            f"{provider} OAuth requires PKCE. Provide code_verifier parameter or use "
            "oauth_get_auth_url which returns pkce_data with code_verifier."
        )

    @rpc_expose(description="Get OAuth authorization URL for any provider")
    def oauth_get_auth_url(
        self,
        provider: str,
        redirect_uri: str = "http://localhost:3000/oauth/callback",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get OAuth authorization URL for any provider.

        Args:
            provider: OAuth provider name
            redirect_uri: OAuth redirect URI
            scopes: Optional list of scopes to request

        Returns:
            Dictionary containing url, state, and optionally pkce_data
        """
        logger.info(f"Generating OAuth authorization URL for provider={provider}")

        # Generate state token
        state = secrets.token_urlsafe(32)

        # Create provider using factory
        provider_instance = self._create_provider(provider, redirect_uri, scopes)

        # Register provider
        self._register_provider(provider_instance)

        # Get authorization URL with PKCE support if needed
        return self._get_authorization_url_with_pkce_support(provider_instance, provider, state)

    @rpc_expose(description="Exchange OAuth authorization code for tokens")
    async def oauth_exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Exchange OAuth authorization code for tokens and store credentials.

        Args:
            provider: OAuth provider name
            code: Authorization code from OAuth callback
            user_email: User email address for credential storage (optional, will be fetched from provider if not provided)
            state: CSRF state token (used to retrieve PKCE data for X)
            redirect_uri: OAuth redirect URI (optional, uses config default if not provided; must match authorization request)
            code_verifier: PKCE code verifier (required for X/Twitter)
            context: Operation context (optional)

        Returns:
            Dictionary containing credential_id, user_email, expires_at, success
        """
        logger.info(
            f"Exchanging OAuth code for provider={provider}, user_email={'provided' if user_email else 'will fetch'}"
        )

        # Create provider using factory
        provider_instance = self._create_provider(provider, redirect_uri)

        # Register provider
        self._register_provider(provider_instance)

        # Exchange code for credential
        try:
            # Check if provider requires PKCE
            factory = self._get_oauth_factory()
            config_name = self._map_provider_name(provider)
            provider_config = factory.get_provider_config(config_name)
            requires_pkce = provider_config and provider_config.requires_pkce

            if requires_pkce:
                pkce_verifier = self._get_pkce_verifier(provider, code_verifier, state)
                credential = await provider_instance.exchange_code_pkce(code, pkce_verifier)
            else:
                credential = await provider_instance.exchange_code(code)
        except ValueError:
            # Re-raise ValueError as-is (e.g., missing PKCE verifier)
            raise
        except Exception as e:
            logger.error(f"Failed to exchange OAuth code: {e}")
            raise ValueError(f"Failed to exchange authorization code: {e}") from e

        # If user_email not provided, try to fetch it from the provider
        if not user_email:
            user_email = await self._get_user_email_from_provider(provider_instance, credential)
            if not user_email:
                raise ValueError(
                    "user_email is required. Could not automatically fetch email from provider. "
                    "Please provide user_email parameter."
                )

        # Store credential
        token_manager = self._get_token_manager()
        tenant_id = get_tenant_id(context)

        # Extract user_id from context (Nexus user identity)
        # This may differ from user_email (OAuth provider email)
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        created_by = current_user_id or user_email

        # Security: Note on permission validation
        # The OAuth flow itself ensures the user_email belongs to the authenticated user
        # We store user_id from context for permission checks, but don't need strict
        # email matching here since the OAuth provider validates ownership during authorization

        # Use provider's actual name for storage
        provider_name = provider_instance.provider_name

        # Set user_email on credential for storage
        credential.user_email = user_email

        try:
            credential_id = await token_manager.store_credential(
                provider=provider_name,
                user_email=user_email,
                credential=credential,
                tenant_id=tenant_id,
                created_by=created_by,
                user_id=current_user_id,  # Pass Nexus user_id for permission checks
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

    async def _get_user_email_from_provider(
        self, provider_instance: Any, credential: Any
    ) -> str | None:
        """Get user email from OAuth provider using the access token.

        Args:
            provider_instance: OAuth provider instance
            credential: OAuth credential with access_token

        Returns:
            User email if found, None otherwise
        """
        import httpx

        provider_name = provider_instance.provider_name

        try:
            # Google: Use tokeninfo endpoint or userinfo endpoint
            if provider_name in ("google-drive", "gmail", "google-cloud-storage"):
                # Try tokeninfo endpoint first (simpler, but may not have email)
                async with httpx.AsyncClient() as client:
                    try:
                        response = await client.get(
                            "https://oauth2.googleapis.com/tokeninfo",
                            params={"access_token": credential.access_token},
                        )
                        response.raise_for_status()
                        token_info = response.json()
                        # tokeninfo may have email if scopes include it
                        if "email" in token_info:
                            email = token_info.get("email")
                            return str(email) if email else None
                    except Exception:
                        pass

                    # If tokeninfo doesn't have email, try userinfo endpoint
                    try:
                        response = await client.get(
                            "https://www.googleapis.com/oauth2/v2/userinfo",
                            headers={"Authorization": f"Bearer {credential.access_token}"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        if "email" in user_info:
                            email = user_info.get("email")
                            return str(email) if email else None
                    except Exception:
                        pass

            # Microsoft: Use Microsoft Graph API
            elif provider_name == "microsoft-onedrive":
                async with httpx.AsyncClient() as client:
                    try:
                        response = await client.get(
                            "https://graph.microsoft.com/v1.0/me",
                            headers={"Authorization": f"Bearer {credential.access_token}"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        if "mail" in user_info:
                            email = user_info.get("mail")
                            return str(email) if email else None
                        elif "userPrincipalName" in user_info:
                            email = user_info.get("userPrincipalName")
                            return str(email) if email else None
                    except Exception:
                        pass

            # X/Twitter: Use X API v2
            elif provider_name == "x":
                async with httpx.AsyncClient() as client:
                    try:
                        response = await client.get(
                            "https://api.twitter.com/2/users/me",
                            headers={"Authorization": f"Bearer {credential.access_token}"},
                            params={"user.fields": "email"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        if "data" in user_info and "email" in user_info["data"]:
                            email = user_info["data"].get("email")
                            return str(email) if email else None
                    except Exception:
                        pass

        except Exception as e:
            logger.warning(f"Failed to fetch user email from provider {provider_name}: {e}")

        return None

    @rpc_expose(description="List all available OAuth providers")
    def oauth_list_providers(
        self,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """List all available OAuth providers from configuration.

        Args:
            context: Operation context (optional)

        Returns:
            List of provider dictionaries containing:
                - name: Provider identifier (e.g., "google-drive", "gmail")
                - display_name: Human-readable name (e.g., "Google Drive", "Gmail")
                - scopes: List of OAuth scopes required
                - requires_pkce: Whether provider requires PKCE
                - icon_url: Optional URL to provider icon/logo
                - metadata: Additional provider-specific metadata
        """
        factory = self._get_oauth_factory()
        providers = []

        for provider_config in factory._oauth_config.providers:
            provider_dict = {
                "name": provider_config.name,
                "display_name": provider_config.display_name,
                "scopes": provider_config.scopes,
                "requires_pkce": provider_config.requires_pkce,
                "metadata": provider_config.metadata,
            }
            if provider_config.icon_url:
                provider_dict["icon_url"] = provider_config.icon_url
            providers.append(provider_dict)

        logger.info(f"Listed {len(providers)} OAuth providers")
        return providers

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

        Note:
            Only returns credentials for the current user (from context).
            Admins can see all credentials in their tenant.
        """
        token_manager = self._get_token_manager()
        # Default to 'default' tenant if not specified to match mount configurations
        tenant_id = get_tenant_id(context)

        # Extract current user's identity from context
        # Use user_id (preferred) or user (legacy) from context
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        is_admin = context and getattr(context, "is_admin", False)

        # List credentials for tenant (and optionally user)
        # Filter by user_id if available (more reliable than email matching)
        credentials = await token_manager.list_credentials(
            tenant_id=tenant_id, user_id=current_user_id if not is_admin else None
        )

        # Filter by provider and revoked status if needed
        result = []
        for cred in credentials:
            # Per-user isolation: non-admins can only see their own credentials
            # Check both user_id (preferred) and user_email (fallback)
            if not is_admin and current_user_id:
                cred_user_id = cred.get("user_id")
                cred_user_email = cred.get("user_email")
                # Match if user_id matches OR (user_id not set and email matches)
                if cred_user_id and cred_user_id != current_user_id:
                    continue
                if not cred_user_id and cred_user_email and cred_user_email != current_user_id:
                    continue
            if provider and cred["provider"] != provider:
                continue
            if not include_revoked and cred.get("revoked", False):
                continue
            result.append(cred)

        logger.info(
            f"Listed {len(result)} OAuth credentials for user_id={current_user_id}, "
            f"tenant={tenant_id}, provider={provider}"
        )
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
            ValueError: If credential not found or user doesn't have permission

        Note:
            Users can only revoke their own credentials unless they are admin.
        """
        token_manager = self._get_token_manager()
        # Default to 'default' tenant if not specified to match mount configurations
        tenant_id = get_tenant_id(context)

        # Extract current user's identity from context
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        is_admin = context and getattr(context, "is_admin", False)

        # Permission check: users can only revoke their own credentials (unless admin)
        # Check by fetching the credential and verifying user_id or user_email match
        if not is_admin and current_user_id:
            # Fetch credential to check ownership
            cred = await token_manager.get_credential(
                provider=provider, user_email=user_email, tenant_id=tenant_id
            )
            if cred:
                # Check if user_id matches (preferred) or user_email matches (fallback)
                stored_user_id = cred.metadata.get("user_id") if cred.metadata else None
                stored_user_email = cred.user_email

                if stored_user_id and stored_user_id != current_user_id:
                    raise ValueError(
                        f"Permission denied: Cannot revoke credentials for {user_email}. "
                        f"Only your own credentials can be revoked."
                    )
                if (
                    not stored_user_id
                    and stored_user_email
                    and stored_user_email != current_user_id
                ):
                    raise ValueError(
                        f"Permission denied: Cannot revoke credentials for {user_email}. "
                        f"Only your own credentials can be revoked."
                    )

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
            ValueError: If credential not found or user doesn't have permission

        Note:
            Users can only test their own credentials unless they are admin.
        """
        token_manager = self._get_token_manager()
        # Default to 'default' tenant if not specified to match mount configurations
        tenant_id = get_tenant_id(context)

        # Extract current user's identity from context
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        is_admin = context and getattr(context, "is_admin", False)

        # Permission check: users can only test their own credentials (unless admin)
        # Check by fetching the credential and verifying user_id or user_email match
        if not is_admin and current_user_id:
            # Fetch credential to check ownership
            cred = await token_manager.get_credential(
                provider=provider, user_email=user_email, tenant_id=tenant_id
            )
            if cred:
                # Check if user_id matches (preferred) or user_email matches (fallback)
                stored_user_id = cred.metadata.get("user_id") if cred.metadata else None
                stored_user_email = cred.user_email

                if stored_user_id and stored_user_id != current_user_id:
                    raise ValueError(
                        f"Permission denied: Cannot test credentials for {user_email}. "
                        f"Only your own credentials can be tested."
                    )
                if (
                    not stored_user_id
                    and stored_user_email
                    and stored_user_email != current_user_id
                ):
                    raise ValueError(
                        f"Permission denied: Cannot test credentials for {user_email}. "
                        f"Only your own credentials can be tested."
                    )

        try:
            # Try to get a valid token (will auto-refresh if needed)
            token = await token_manager.get_valid_token(
                provider=provider,
                user_email=user_email,
                tenant_id=tenant_id,
            )

            if token:
                # Get credential details (returns list of dicts)
                credentials = await token_manager.list_credentials(
                    tenant_id=tenant_id, user_email=user_email
                )
                cred_dict = next(
                    (c for c in credentials if c.get("user_email") == user_email),
                    None,
                )

                logger.info(f"OAuth credential test successful for {provider}:{user_email}")
                return {
                    "valid": True,
                    "refreshed": True,  # If we got here, token was valid or refreshed
                    "expires_at": cred_dict.get("expires_at") if cred_dict else None,
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

    @rpc_expose(description="Connect to MCP provider via Klavis")
    async def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Connect to an MCP provider using Klavis hosted OAuth.

        This method:
        1. Creates a Klavis MCP instance for the provider
        2. If we have stored OAuth tokens for this provider, passes them to Klavis via set_auth()
        3. Returns the OAuth URL if not authenticated, or strata URL if authenticated
        4. Generates SKILL.md in user's skill folder

        Args:
            provider: MCP provider name (e.g., "google_drive", "gmail", "slack")
            redirect_url: OAuth redirect URL (for OAuth flow)
            context: Operation context

        Returns:
            Dict with:
                - provider: Provider name
                - oauth_url: URL to complete OAuth (if not authenticated)
                - strata_url: MCP strata URL (if authenticated)
                - is_authenticated: Whether user is authenticated
                - tools: List of available MCP tools (if authenticated)
                - skill_path: Path to generated SKILL.md
        """
        import os

        import httpx

        from nexus.mcp.oauth_mappings import OAuthKlavisMappings

        klavis_api_key = os.environ.get("KLAVIS_API_KEY")
        if not klavis_api_key:
            raise ValueError("KLAVIS_API_KEY environment variable not set")

        # Get user info from context
        user_id = "admin"
        if context:
            user_id = getattr(context, "user_id", None) or getattr(context, "user", None) or "admin"

        # Create unique Klavis user ID for this Nexus user
        klavis_user_id = f"nexus-{user_id}"

        headers = {
            "Authorization": f"Bearer {klavis_api_key}",
            "Content-Type": "application/json",
        }

        oauth_mappings = OAuthKlavisMappings()

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Check for stored OAuth credentials and pass to Klavis via set_auth
            oauth_provider = oauth_mappings.get_oauth_provider_for_klavis_mcp(provider)
            used_stored_token = False

            if oauth_provider:
                # Try to find a stored credential for this OAuth provider
                try:
                    token_manager = self._get_token_manager()
                    credential = None

                    # Get the mapping to find all local provider names
                    mapping = oauth_mappings.get_mapping(oauth_provider)
                    local_providers = mapping.local_providers if mapping else [oauth_provider]
                    logger.info(
                        f"Looking for credentials: oauth_provider={oauth_provider}, local_providers={local_providers}, user_id={user_id}"
                    )

                    # Try listing credentials to find one for this provider
                    credentials = await token_manager.list_credentials(user_id=user_id)
                    logger.info(f"Found {len(credentials)} credentials for user_id={user_id}")
                    for cred_info in credentials:
                        logger.debug(
                            f"  Credential: provider={cred_info.get('provider')}, user_email={cred_info.get('user_email')}, user_id={cred_info.get('user_id')}"
                        )

                    # Also try without user_id filter (fallback)
                    if not credentials:
                        logger.info(
                            "No credentials found with user_id filter, trying without filter"
                        )
                        credentials = await token_manager.list_credentials()
                        logger.info(f"Found {len(credentials)} total credentials")
                        for cred_info in credentials:
                            logger.debug(
                                f"  Credential: provider={cred_info.get('provider')}, user_email={cred_info.get('user_email')}, user_id={cred_info.get('user_id')}"
                            )

                    for cred_info in credentials:
                        cred_provider = cred_info.get("provider")
                        # Check if credential provider matches our oauth provider or any of its local names
                        if cred_provider == oauth_provider or cred_provider in local_providers:
                            user_email = cred_info.get("user_email")
                            if user_email:
                                credential = await token_manager.get_credential(
                                    provider=cred_provider,
                                    user_email=user_email,
                                )
                            if credential:
                                logger.info(
                                    f"Found matching credential for {cred_provider}:{cred_info.get('user_email')}"
                                )
                                break

                    if credential and credential.access_token:
                        # Pass our token to Klavis via set_auth
                        logger.info(
                            f"Passing stored {oauth_provider} token to Klavis for {provider}"
                        )
                        set_auth_resp = await client.post(
                            "https://api.klavis.ai/user/set-auth",
                            json={
                                "serverName": provider,
                                "userId": klavis_user_id,
                                "authData": {
                                    "data": {
                                        "access_token": credential.access_token,
                                        "token_type": "Bearer",
                                        "refresh_token": credential.refresh_token,
                                    }
                                },
                            },
                            headers=headers,
                        )
                        if set_auth_resp.status_code == 200:
                            logger.info(f"Successfully passed token to Klavis for {provider}")
                            used_stored_token = True
                        else:
                            logger.warning(f"Failed to pass token to Klavis: {set_auth_resp.text}")
                except Exception as e:
                    logger.debug(f"Could not pass stored token to Klavis: {e}")

            # Step 2: Create MCP instance
            create_resp = await client.post(
                "https://api.klavis.ai/mcp-server/instance/create",
                json={
                    "serverName": provider,
                    "userId": klavis_user_id,
                },
                headers=headers,
            )
            create_resp.raise_for_status()
            instance_data = create_resp.json()
            instance_id = instance_data.get("instanceId")
            # Get server URL from instance creation response
            server_url = instance_data.get("serverUrl") or instance_data.get("url")
            logger.info(f"Klavis instance created: id={instance_id}, server_url={server_url}")
            logger.debug(f"Klavis instance data: {instance_data}")

            # Step 3: Get instance status to check if authenticated
            status_resp = await client.get(
                f"https://api.klavis.ai/mcp-server/instance/{instance_id}",
                headers=headers,
            )
            status_resp.raise_for_status()
            status_data = status_resp.json()
            logger.debug(f"Klavis instance status: {status_data}")
            # Also try to get server URL from status if not in create response
            if not server_url:
                server_url = status_data.get("serverUrl") or status_data.get("url")

            is_authenticated = status_data.get("isAuthenticated", False)

            # If not authenticated after passing token, return OAuth URL
            if not is_authenticated:
                oauth_url = status_data.get("oauthUrl")
                if redirect_url and oauth_url:
                    # Append redirect_url to oauth_url
                    separator = "&" if "?" in oauth_url else "?"
                    oauth_url = f"{oauth_url}{separator}redirect_url={redirect_url}"

                logger.info(f"MCP OAuth URL generated for {provider}, user={klavis_user_id}")
                return {
                    "provider": provider,
                    "instance_id": instance_id,
                    "oauth_url": oauth_url,
                    "is_authenticated": False,
                    "used_stored_token": used_stored_token,
                    "user_id": klavis_user_id,
                }

            # Step 4: Get strata URL for authenticated user (optional - may not be needed)
            strata_url = None
            try:
                strata_resp = await client.post(
                    "https://api.klavis.ai/mcp-server/strata/create",
                    json={
                        "serverName": provider,
                        "userId": klavis_user_id,
                    },
                    headers=headers,
                )
                if strata_resp.status_code == 200:
                    strata_data = strata_resp.json()
                    strata_url = strata_data.get("strataUrl")
                else:
                    logger.warning(
                        f"Klavis strata/create returned {strata_resp.status_code}: {strata_resp.text}"
                    )
            except Exception as e:
                logger.warning(f"Klavis strata/create failed: {e}")

            # Step 5: Get available tools
            tools = []
            # Build request - use serverUrl if available, otherwise serverName
            list_tools_payload: dict[str, Any] = {"userId": klavis_user_id}
            if server_url:
                list_tools_payload["serverUrl"] = server_url
            else:
                list_tools_payload["serverName"] = provider

            tools_resp = await client.post(
                "https://api.klavis.ai/mcp-server/list-tools",
                json=list_tools_payload,
                headers=headers,
            )
            logger.info(f"Klavis list-tools response: {tools_resp.status_code}")
            if tools_resp.status_code == 200:
                tools_data = tools_resp.json()
                logger.info(
                    f"Klavis list-tools data: success={tools_data.get('success')}, tools_count={len(tools_data.get('tools', []))}"
                )
                if tools_data.get("success"):
                    tools = tools_data.get("tools", [])
                else:
                    logger.warning(f"Klavis list-tools returned success=False: {tools_data}")
            else:
                logger.warning(
                    f"Klavis list-tools failed: {tools_resp.status_code} - {tools_resp.text}"
                )

            # Step 6: Generate SKILL.md, mount.json, and {tool}.json files in user's folder
            # generate_skill_md uses ServiceMap to get connector template + MCP tools
            import json as json_module
            from datetime import UTC, datetime

            from nexus.backends.service_map import ServiceMap
            from nexus.skills.mcp_models import MCPMount, MCPToolConfig, MCPToolDefinition
            from nexus.skills.skill_generator import generate_skill_md

            service_name = ServiceMap.get_service_name(mcp=provider) or provider
            skill_base_path = f"/skills/users/{user_id}/"
            skill_path = f"{skill_base_path}{service_name}/"
            skill_file = f"{skill_path}SKILL.md"
            mount_file = f"{skill_path}mount.json"

            # Find connector mount path if connector exists for this service
            # This makes the SKILL.md reference the actual data location
            service_info = ServiceMap.get_service_info(service_name)
            data_mount_path = skill_path  # default to skill path for MCP-only services
            logger.info(
                f"Looking for connector mount: service={service_name}, connector={service_info.connector if service_info else None}"
            )
            if service_info and service_info.connector and hasattr(self, "router"):
                # Look for existing mount with this connector
                router_mounts = getattr(self.router, "_mounts", [])
                logger.info(f"Found {len(router_mounts)} mounts in router")
                # Normalize connector name for comparison (e.g., gdrive_connector -> gdriveconnector, googledrive)
                connector_variants = [
                    service_info.connector.lower().replace("_", ""),  # gdriveconnector
                    service_info.connector.lower().replace("_connector", ""),  # gdrive
                    "googledrive",  # common variant
                ]
                for mount in router_mounts:
                    mount_backend = getattr(mount, "backend", None)
                    mount_point = getattr(mount, "mount_point", None)
                    if mount_backend:
                        backend_type = type(mount_backend).__name__.lower()
                        logger.info(f"  Mount: {mount_point} -> {backend_type}")
                        # Check if any variant matches
                        for variant in connector_variants:
                            if variant in backend_type:
                                if mount_point is not None:
                                    data_mount_path = str(mount_point)
                                    logger.info(
                                        f"Found connector mount at {data_mount_path} (matched {variant})"
                                    )
                                break
                        if data_mount_path != skill_path:
                            break
            logger.info(f"Using mount path for SKILL.md: {data_mount_path}")

            skill_md = generate_skill_md(
                service_name=service_name,
                mount_path=data_mount_path,
                mcp_tools=tools,
            )

            # Write skill files
            try:
                if hasattr(self, "mkdir"):
                    self.mkdir(skill_path, parents=True, exist_ok=True, context=context)

                if hasattr(self, "write"):
                    # Write SKILL.md
                    self.write(skill_file, skill_md.encode("utf-8"), context=context)
                    logger.info(f"Generated MCP skill: {skill_file}")

                    # Write mount.json
                    now = datetime.now(UTC)
                    mount_config = MCPMount(
                        name=service_name,
                        description=service_info.description
                        if service_info
                        else f"{provider} MCP integration",
                        transport="klavis_rest",
                        url=server_url or strata_url,
                        klavis_strata_id=instance_id,
                        auth_type="oauth",
                        auth_config={
                            "klavis_user_id": klavis_user_id
                        },  # Store user ID for API calls
                        tools_path=skill_path,
                        mounted=True,
                        mounted_at=now,
                        last_sync=now,
                        tool_count=len(tools),
                        tools=[t.get("name", "") for t in tools],
                        tier="user",
                    )
                    mount_json = json_module.dumps(mount_config.to_dict(), indent=2)
                    self.write(mount_file, mount_json.encode("utf-8"), context=context)
                    logger.info(f"Generated mount config: {mount_file}")

                    # Write {tool}.json for each tool
                    for tool in tools:
                        tool_name = tool.get("name", "")
                        if not tool_name:
                            continue

                        tool_config = MCPToolConfig(
                            endpoint=f"mcp://{service_name}/{tool_name}",
                            input_schema=tool.get("inputSchema", {}),
                            requires_mount=True,
                            mount_name=service_name,
                            when_to_use=tool.get("description", ""),
                        )

                        tool_def = MCPToolDefinition(
                            name=tool_name,
                            description=tool.get("description", ""),
                            version="1.0.0",
                            skill_type="mcp_tool",
                            mcp_config=tool_config,
                            created_at=now,
                            modified_at=now,
                        )

                        tool_file = f"{skill_path}{tool_name}.json"
                        tool_json = json_module.dumps(tool_def.to_dict(), indent=2)
                        self.write(tool_file, tool_json.encode("utf-8"), context=context)

                    logger.info(f"Generated {len(tools)} tool definitions in {skill_path}")

            except Exception as e:
                logger.warning(f"Failed to write skill files: {e}")

            return {
                "provider": provider,
                "instance_id": instance_id,
                "strata_url": strata_url,
                "is_authenticated": True,
                "tools": tools,
                "tool_count": len(tools),
                "skill_path": skill_file,
                "mount_path": mount_file,
                "tools_path": skill_path,
                "user_id": klavis_user_id,
            }
