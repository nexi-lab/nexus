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
        - Supports multi-tenant credential isolation
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
    def oauth_list_providers(
        self,
        context: OperationContext | None = None,
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
        # TODO: Extract oauth_list_providers implementation
        raise NotImplementedError(
            "oauth_list_providers() not yet implemented - Phase 2 in progress"
        )

    # =========================================================================
    # Public API: OAuth Flow
    # =========================================================================

    @rpc_expose(description="Get OAuth authorization URL for any provider")
    def oauth_get_auth_url(
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
        # TODO: Extract oauth_get_auth_url implementation
        raise NotImplementedError("oauth_get_auth_url() not yet implemented - Phase 2 in progress")

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
            context: Operation context for tenant isolation

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
            - Credentials are stored per-tenant for isolation
        """
        # TODO: Extract oauth_exchange_code implementation
        raise NotImplementedError("oauth_exchange_code() not yet implemented - Phase 2 in progress")

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
        in their tenant.

        Args:
            provider: Optional provider filter (e.g., "google")
            include_revoked: Include revoked credentials (default: False)
            context: Operation context for user/tenant identification

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
            - Admins see all credentials in their tenant
            - Credentials from other tenants are never visible
        """
        # TODO: Extract oauth_list_credentials implementation
        raise NotImplementedError(
            "oauth_list_credentials() not yet implemented - Phase 2 in progress"
        )

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
            - Admins can revoke any credential in their tenant
            - Revoked credentials cannot be unrevoked (create new credential instead)
        """
        # TODO: Extract oauth_revoke_credential implementation
        raise NotImplementedError(
            "oauth_revoke_credential() not yet implemented - Phase 2 in progress"
        )

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
        # TODO: Extract oauth_test_credential implementation
        raise NotImplementedError(
            "oauth_test_credential() not yet implemented - Phase 2 in progress"
        )

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
        # TODO: Extract mcp_connect implementation
        raise NotImplementedError("mcp_connect() not yet implemented - Phase 2 in progress")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_oauth_factory(self) -> Any:
        """Get or create OAuth provider factory.

        Returns:
            OAuthProviderFactory instance
        """
        # TODO: Extract factory getter
        pass

    def _get_token_manager(self) -> Any:
        """Get or create TokenManager instance.

        Returns:
            TokenManager instance
        """
        # TODO: Extract token manager getter
        pass

    def _map_provider_name(self, provider: str) -> str:
        """Map user-facing provider name to config provider name.

        Args:
            provider: User-facing provider name (e.g., "google", "microsoft")

        Returns:
            Config provider name (e.g., "google-drive", "microsoft-onedrive")
        """
        # TODO: Extract provider name mapping
        return provider

    def _create_provider(
        self,
        _provider: str,
        _redirect_uri: str | None = None,
        _scopes: builtins.list[str] | None = None,
    ) -> Any:
        """Create OAuth provider instance using factory.

        Args:
            _provider: User-facing provider name
            _redirect_uri: OAuth redirect URI
            _scopes: Optional scopes

        Returns:
            OAuthProvider instance
        """
        # TODO: Extract provider creation
        pass

    def _register_provider(self, _provider_instance: Any) -> None:
        """Register provider with TokenManager.

        Args:
            _provider_instance: OAuthProvider instance to register
        """
        # TODO: Extract provider registration
        pass


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
# 4. [ ] Extract oauth_list_credentials() - Credential listing with tenant isolation
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
