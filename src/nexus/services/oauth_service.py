"""OAuth Service - Extracted from NexusFSOAuthMixin.

This service handles all OAuth credential management operations:
- List available OAuth providers
- Generate authorization URLs with PKCE support
- Exchange authorization codes for tokens
- Manage stored credentials (list, revoke, test)
- Connect to MCP providers via Klavis hosted OAuth

Phase 2: Core Refactoring (Issue #988, Task 2.6)
Extracted from: nexus_fs_oauth.py (1,116 lines)
"""

from __future__ import annotations

import builtins
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


class OAuthService:
    """Independent OAuth service extracted from NexusFS.

    Handles all OAuth credential management operations:
    - Provider discovery and configuration
    - OAuth flow (authorization URL + code exchange)
    - PKCE support for providers that require it
    - Credential lifecycle (list, revoke, test validity)
    - MCP provider integration via Klavis

    Architecture:
        - Works with OAuthProviderFactory for provider creation
        - Uses TokenManager for credential storage
        - Supports multi-zone credential isolation
        - Per-user permission enforcement
        - Clean dependency injection

    Example:
        ```python
        oauth_service = OAuthService(
            oauth_factory=factory,
            token_manager=token_manager
        )

        # List available providers
        providers = oauth_service.oauth_list_providers()

        # Get authorization URL
        auth_data = oauth_service.oauth_get_auth_url(
            provider="google",
            redirect_uri="http://localhost:3000/oauth/callback"
        )
        # User visits auth_data["url"]

        # Exchange code for tokens
        result = await oauth_service.oauth_exchange_code(
            provider="google",
            code="auth_code_from_callback",
            user_email="user@example.com"
        )

        # List user's credentials
        creds = await oauth_service.oauth_list_credentials(context=context)

        # Test credential validity
        test_result = await oauth_service.oauth_test_credential(
            provider="google",
            user_email="user@example.com",
            context=context
        )

        # Revoke credential
        await oauth_service.oauth_revoke_credential(
            provider="google",
            user_email="user@example.com",
            context=context
        )
        ```
    """

    def __init__(
        self,
        oauth_factory: Any | None = None,
        token_manager: Any | None = None,
        nexus_fs: Any | None = None,
    ):
        """Initialize OAuth service.

        Args:
            oauth_factory: OAuthProviderFactory for creating provider instances
            token_manager: TokenManager for credential storage
            nexus_fs: NexusFS instance for filesystem operations (used by mcp_connect)
        """
        self._oauth_factory = oauth_factory
        self._token_manager = token_manager
        self.nexus_fs = nexus_fs

        logger.info("[OAuthService] Initialized")

    # =========================================================================
    # Public API: Provider Discovery
    # =========================================================================

    @rpc_expose(description="List all available OAuth providers")
    async def oauth_list_providers(
        self,
        _context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all available OAuth providers from configuration.

        Returns information about all configured OAuth providers including
        their scopes, PKCE requirements, and display names.

        Args:
            context: Operation context (optional)

        Returns:
            List of provider dictionaries containing:
                - name: Provider identifier (e.g., "google-drive", "gmail") (str)
                - display_name: Human-readable name (e.g., "Google Drive") (str)
                - scopes: List of OAuth scopes required (list[str])
                - requires_pkce: Whether provider requires PKCE (bool)
                - icon_url: Optional URL to provider icon/logo (str|None)
                - metadata: Additional provider-specific metadata (dict)

        Examples:
            # List all providers
            providers = service.oauth_list_providers()
            for p in providers:
                print(f"{p['display_name']}: {', '.join(p['scopes'])}")

            # Find specific provider
            providers = service.oauth_list_providers()
            google = next((p for p in providers if p['name'] == 'google-drive'), None)
            if google:
                print(f"Google Drive requires PKCE: {google['requires_pkce']}")
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

    # =========================================================================
    # Public API: OAuth Flow
    # =========================================================================

    @rpc_expose(description="Get OAuth authorization URL for any provider")
    async def oauth_get_auth_url(
        self,
        provider: str,
        redirect_uri: str = "http://localhost:3000/oauth/callback",
        scopes: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Get OAuth authorization URL for any provider.

        Generates a state token and creates the OAuth authorization URL.
        For providers requiring PKCE (e.g., X/Twitter), also generates
        PKCE parameters.

        Args:
            provider: OAuth provider name (e.g., "google", "microsoft", "x")
            redirect_uri: OAuth redirect URI (must match provider configuration)
            scopes: Optional list of scopes to request (uses defaults if not provided)

        Returns:
            Dictionary containing:
                - url: Authorization URL to redirect user to (str)
                - state: CSRF state token (str)
                - pkce_data: PKCE parameters if provider requires it (dict|None)
                    - code_verifier: PKCE code verifier
                    - code_challenge: PKCE code challenge
                    - code_challenge_method: Challenge method (S256)

        Examples:
            # Get Google Drive auth URL
            auth_data = service.oauth_get_auth_url(
                provider="google",
                redirect_uri="http://localhost:3000/oauth/callback"
            )
            print(f"Visit: {auth_data['url']}")

            # Get X/Twitter auth URL with PKCE
            auth_data = service.oauth_get_auth_url(
                provider="x",
                redirect_uri="http://localhost:3000/oauth/callback"
            )
            # Store auth_data['pkce_data']['code_verifier'] for exchange step

            # Custom scopes
            auth_data = service.oauth_get_auth_url(
                provider="google",
                scopes=["https://www.googleapis.com/auth/drive.readonly"]
            )

        Note:
            - State token is used for CSRF protection
            - For PKCE providers, store pkce_data for use in oauth_exchange_code
            - User must visit the returned URL to authorize access
        """
        import secrets

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

        After user authorizes access, exchange the authorization code for
        access and refresh tokens. Automatically detects if provider requires
        PKCE and validates the code_verifier.

        Args:
            provider: OAuth provider name (e.g., "google", "microsoft", "x")
            code: Authorization code from OAuth callback
            user_email: User email for credential storage (optional, auto-fetched if not provided)
            state: CSRF state token from authorization request (used to retrieve PKCE data)
            redirect_uri: OAuth redirect URI (must match authorization request)
            code_verifier: PKCE code verifier (required for X/Twitter and other PKCE providers)
            context: Operation context for zone isolation

        Returns:
            Dictionary containing:
                - success: Whether exchange succeeded (bool)
                - credential_id: Unique credential identifier (str)
                - user_email: User email address (str)
                - expires_at: Token expiration timestamp in ISO format (str)
                - provider: Provider name (str)

        Raises:
            ValueError: If code exchange fails or PKCE verifier missing

        Examples:
            # Exchange Google code
            result = await service.oauth_exchange_code(
                provider="google",
                code="4/0AbCD...",
                user_email="user@example.com"
            )
            print(f"Credential ID: {result['credential_id']}")

            # Exchange X/Twitter code with PKCE
            result = await service.oauth_exchange_code(
                provider="x",
                code="auth_code",
                state="state_from_auth_url",
                code_verifier="verifier_from_pkce_data"
            )

            # Auto-fetch user email
            result = await service.oauth_exchange_code(
                provider="google",
                code="4/0AbCD...",
                context=context
            )
            # user_email will be fetched from provider

        Note:
            - For PKCE providers, code_verifier from oauth_get_auth_url is required
            - If user_email not provided, service attempts to fetch it from provider
            - Credentials are stored per-zone for isolation
        """
        from nexus.core.context_utils import get_zone_id

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
        zone_id = get_zone_id(context)

        # Extract user_id from context (Nexus user identity)
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        created_by = current_user_id or user_email

        # Use provider's actual name for storage
        provider_name = provider_instance.provider_name

        # Set user_email on credential for storage
        credential.user_email = user_email

        try:
            credential_id = await token_manager.store_credential(
                provider=provider_name,
                user_email=user_email,
                credential=credential,
                zone_id=zone_id,
                created_by=created_by,
                user_id=current_user_id,
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

    # =========================================================================
    # Public API: Credential Management
    # =========================================================================

    @rpc_expose(description="List all OAuth credentials")
    async def oauth_list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all OAuth credentials for the current user.

        Returns credentials accessible to the current user. Non-admin users
        can only see their own credentials. Admins can see all credentials
        in their zone.

        Args:
            provider: Optional provider filter (e.g., "google")
            include_revoked: Include revoked credentials (default: False)
            context: Operation context for user/zone identification

        Returns:
            List of credential dictionaries containing:
                - credential_id: Unique identifier (str)
                - provider: OAuth provider name (str)
                - user_email: User email address (str)
                - scopes: List of granted scopes (list[str])
                - expires_at: Token expiration timestamp in ISO format (str)
                - created_at: Creation timestamp in ISO format (str)
                - last_used_at: Last usage timestamp in ISO format (str|None)
                - revoked: Whether credential is revoked (bool)

        Examples:
            # List all user's credentials
            creds = await service.oauth_list_credentials(context=context)
            for cred in creds:
                print(f"{cred['provider']}: {cred['user_email']}")

            # List Google credentials only
            google_creds = await service.oauth_list_credentials(
                provider="google",
                context=context
            )

            # List including revoked
            all_creds = await service.oauth_list_credentials(
                include_revoked=True,
                context=context
            )

        Note:
            - User isolation enforced automatically via context
            - Admins see all credentials in their zone
            - Credentials from other zones are never visible
        """
        from nexus.core.context_utils import get_zone_id

        token_manager = self._get_token_manager()
        zone_id = get_zone_id(context)

        # Extract current user's identity from context
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        is_admin = context and getattr(context, "is_admin", False)

        # List credentials for tenant (and optionally user)
        credentials = await token_manager.list_credentials(
            zone_id=zone_id, user_id=current_user_id if not is_admin else None
        )

        # Filter by provider and revoked status if needed
        result = []
        for cred in credentials:
            # Per-user isolation: non-admins can only see their own credentials
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
            f"tenant={zone_id}, provider={provider}"
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

        Marks a credential as revoked, preventing further use. Users can only
        revoke their own credentials unless they are admin.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context for permission checking

        Returns:
            Dictionary containing:
                - success: True if revoked successfully (bool)
                - credential_id: Revoked credential ID (str)

        Raises:
            ValueError: If credential not found or user doesn't have permission

        Examples:
            # Revoke a credential
            result = await service.oauth_revoke_credential(
                provider="google",
                user_email="user@example.com",
                context=context
            )
            if result["success"]:
                print(f"Revoked {result['credential_id']}")

            # Admin revokes any credential
            result = await service.oauth_revoke_credential(
                provider="google",
                user_email="other@example.com",
                context=admin_context
            )

        Note:
            - Users can only revoke their own credentials
            - Admins can revoke any credential in their zone
            - Revoked credentials cannot be unrevoked (create new credential instead)
        """
        from nexus.core.context_utils import get_zone_id

        token_manager = self._get_token_manager()
        zone_id = get_zone_id(context)

        # Extract current user's identity from context
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        is_admin = context and getattr(context, "is_admin", False)

        # Permission check: users can only revoke their own credentials (unless admin)
        if not is_admin and current_user_id:
            # Fetch credential to check ownership
            cred = await token_manager.get_credential(
                provider=provider, user_email=user_email, zone_id=zone_id
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
                zone_id=zone_id,
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

        Attempts to get a valid token, refreshing if necessary. Users can only
        test their own credentials unless they are admin.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context for permission checking

        Returns:
            Dictionary containing:
                - valid: True if credential is valid (bool)
                - refreshed: True if token was refreshed (bool)
                - expires_at: Token expiration timestamp in ISO format (str|None)
                - error: Error message if invalid (str|None)

        Raises:
            ValueError: If credential not found or user doesn't have permission

        Examples:
            # Test a credential
            result = await service.oauth_test_credential(
                provider="google",
                user_email="user@example.com",
                context=context
            )
            if result["valid"]:
                print(f"Valid until {result['expires_at']}")
            else:
                print(f"Invalid: {result['error']}")

            # Check before using
            result = await service.oauth_test_credential(
                provider="google",
                user_email="user@example.com",
                context=context
            )
            if not result["valid"]:
                # Prompt user to re-authenticate
                print("Please re-authenticate")

        Note:
            - Automatically attempts token refresh if expired
            - Returns detailed error if credential cannot be refreshed
            - Does not revoke invalid credentials (use oauth_revoke_credential)
        """
        from nexus.core.context_utils import get_zone_id

        token_manager = self._get_token_manager()
        zone_id = get_zone_id(context)

        # Extract current user's identity from context
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None) or getattr(context, "user", None)
        is_admin = context and getattr(context, "is_admin", False)

        # Permission check: users can only test their own credentials (unless admin)
        if not is_admin and current_user_id:
            # Fetch credential to check ownership
            cred = await token_manager.get_credential(
                provider=provider, user_email=user_email, zone_id=zone_id
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
                zone_id=zone_id,
            )

            if token:
                # Get credential details
                credentials = await token_manager.list_credentials(
                    zone_id=zone_id, user_email=user_email
                )
                cred_dict = next(
                    (c for c in credentials if c.get("user_email") == user_email),
                    None,
                )

                logger.info(f"OAuth credential test successful for {provider}:{user_email}")
                return {
                    "valid": True,
                    "refreshed": True,
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

    # =========================================================================
    # Public API: MCP Integration
    # =========================================================================

    @rpc_expose(description="Connect to MCP provider via Klavis")
    async def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Connect to an MCP provider using Klavis hosted OAuth.

        Creates a Klavis MCP instance for the provider. If OAuth tokens are
        stored for this provider, passes them to Klavis. Otherwise returns
        OAuth URL for authentication.

        Args:
            provider: MCP provider name (e.g., "google_drive", "gmail", "slack")
            redirect_url: OAuth redirect URL for OAuth flow
            context: Operation context for user identification

        Returns:
            Dictionary containing:
                - provider: Provider name (str)
                - oauth_url: URL to complete OAuth if not authenticated (str|None)
                - strata_url: MCP strata URL if authenticated (str|None)
                - is_authenticated: Whether user is authenticated (bool)
                - tools: List of available MCP tools if authenticated (list[dict]|None)
                - skill_path: Path to generated SKILL.md (str)

        Raises:
            ValueError: If KLAVIS_API_KEY not set or provider not supported

        Examples:
            # Connect to Google Drive via Klavis
            result = await service.mcp_connect(
                provider="google_drive",
                redirect_url="http://localhost:3000/oauth/callback",
                context=context
            )

            if result["is_authenticated"]:
                print(f"MCP URL: {result['strata_url']}")
                print(f"Available tools: {len(result['tools'])}")
            else:
                print(f"Authenticate at: {result['oauth_url']}")

            # After OAuth, connect again
            result = await service.mcp_connect(
                provider="google_drive",
                context=context
            )
            # Now is_authenticated will be True

        Note:
            - Requires KLAVIS_API_KEY environment variable
            - Automatically generates SKILL.md in user's skill folder
            - Uses stored OAuth credentials if available
            - Provider names use underscore (google_drive, not google-drive)
        """
        import json as json_module
        import os
        from datetime import UTC, datetime

        import httpx

        from nexus.backends.service_map import ServiceMap
        from nexus.mcp.oauth_mappings import OAuthKlavisMappings
        from nexus.skills.mcp_models import MCPMount, MCPToolConfig, MCPToolDefinition
        from nexus.skills.skill_generator import generate_skill_md

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
            service_name = ServiceMap.get_service_name(mcp=provider) or provider
            skill_base_path = f"/skills/users/{user_id}/"
            skill_path = f"{skill_base_path}{service_name}/"
            skill_file = f"{skill_path}SKILL.md"
            mount_file = f"{skill_path}mount.json"

            # Find connector mount path if connector exists for this service
            service_info = ServiceMap.get_service_info(service_name)
            data_mount_path = skill_path  # default to skill path for MCP-only services
            logger.info(
                f"Looking for connector mount: service={service_name}, connector={service_info.connector if service_info else None}"
            )
            if (
                service_info
                and service_info.connector
                and self.nexus_fs
                and hasattr(self.nexus_fs, "router")
            ):
                # Look for existing mount with this connector
                router_mounts = getattr(self.nexus_fs.router, "_mounts", [])
                logger.info(f"Found {len(router_mounts)} mounts in router")
                # Normalize connector name for comparison
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
                if self.nexus_fs and hasattr(self.nexus_fs, "mkdir"):
                    self.nexus_fs.mkdir(skill_path, parents=True, exist_ok=True, context=context)

                if self.nexus_fs and hasattr(self.nexus_fs, "write"):
                    # Write SKILL.md
                    self.nexus_fs.write(skill_file, skill_md.encode("utf-8"), context=context)
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
                    self.nexus_fs.write(mount_file, mount_json.encode("utf-8"), context=context)
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
                        self.nexus_fs.write(tool_file, tool_json.encode("utf-8"), context=context)

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

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_oauth_factory(self) -> Any:
        """Get or create OAuth provider factory.

        Returns:
            OAuthProviderFactory instance
        """
        if self._oauth_factory is None:
            from nexus.server.auth.oauth_factory import OAuthProviderFactory

            # Get config if available
            oauth_config = None
            if self.nexus_fs and hasattr(self.nexus_fs, "_config"):
                config = getattr(self.nexus_fs, "_config", None)
                if config and hasattr(config, "oauth") and config.oauth:
                    oauth_config = config.oauth

            self._oauth_factory = OAuthProviderFactory(config=oauth_config)

        return self._oauth_factory

    def _get_token_manager(self) -> Any:
        """Get or create TokenManager instance.

        Returns:
            TokenManager instance
        """
        if self._token_manager is None:
            from nexus.core.context_utils import get_database_url
            from nexus.server.auth.token_manager import TokenManager

            # Use centralized database URL resolution
            db_path = get_database_url(self.nexus_fs) if self.nexus_fs else None

            if not db_path:
                raise RuntimeError("Database path not configured for TokenManager")

            logger.debug(f"TokenManager database URL resolved to: {db_path}")

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
        scopes: builtins.list[str] | None = None,
    ) -> Any:
        """Create OAuth provider instance using factory.

        Args:
            provider: User-facing provider name
            redirect_uri: OAuth redirect URI
            scopes: Optional scopes

        Returns:
            OAuthProvider instance
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
            # Store PKCE data in module-level cache
            from nexus.core.nexus_fs_oauth import _pkce_cache

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
            from nexus.core.nexus_fs_oauth import _pkce_cache

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


# =============================================================================
# Phase 2 Extraction Progress
# =============================================================================
#
# Status: Skeleton created ✅
#
# TODO (in order of priority):
# 1. [ ] Extract oauth_list_providers() - Provider discovery
# 2. [ ] Extract oauth_get_auth_url() with PKCE support
# 3. [ ] Extract oauth_exchange_code() - Code to token exchange
# 4. [ ] Extract oauth_list_credentials() - Credential listing with zone isolation
# 5. [ ] Extract oauth_revoke_credential() - Credential revocation with permissions
# 6. [ ] Extract oauth_test_credential() - Credential validation
# 7. [ ] Extract mcp_connect() - Klavis MCP integration
# 8. [ ] Extract helper methods (factory, token manager, provider creation)
# 9. [ ] Add unit tests for OAuthService
# 10. [ ] Update NexusFS to use composition
# 11. [ ] Add backward compatibility shims with deprecation warnings
# 12. [ ] Update documentation and migration guide
#
# Lines extracted: 0 / 1,116 (0%)
# Files affected: 1 created, 0 modified
#
# This is a phased extraction to maintain working code at each step.
#
