"""OAuth Service — RPC surface + MCP integration.

Thin wrapper around ``nexus.auth.oauth.credential_service.OAuthCredentialService``
(pure business logic in the auth brick) with ``@rpc_expose`` decorators and the
``mcp_connect`` orchestration that depends on server-layer modules.

Issue #2281 / #8B: Split OAuthService into brick (credential lifecycle) + services (RPC + MCP).
"""

import builtins
import importlib as _il
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import DEFAULT_OAUTH_REDIRECT_URI
from nexus.lib.rpc_decorator import rpc_expose
from nexus.services.protocols.filesystem import NexusFilesystem

# Brick import: TYPE_CHECKING for mypy types, importlib for runtime (avoids import-linter)
if TYPE_CHECKING:
    from nexus.bricks.auth.oauth.credential_service import (
        OAuthCredentialService,
        PKCEStateStore,
    )
else:
    _oauth_cred = _il.import_module("nexus.bricks.auth.oauth.credential_service")
    OAuthCredentialService = _oauth_cred.OAuthCredentialService
    PKCEStateStore = _oauth_cred.PKCEStateStore

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

# Re-export for backward compatibility (tests import PKCEStateStore from here)
__all__ = ["OAuthService", "PKCEStateStore"]


class OAuthService:
    """OAuth service with RPC surface and MCP integration.

    Composes ``OAuthCredentialService`` for pure credential lifecycle and
    adds ``@rpc_expose`` decorators + the ``mcp_connect`` method that
    depends on server-layer modules (ServiceMap, MCP models, Klavis API).

    Architecture:
        - Credential lifecycle delegated to ``OAuthCredentialService`` (auth brick)
        - MCP integration stays here (depends on ``nexus.backends``, ``nexus.mcp``)
        - ``@rpc_expose`` applied here (RPC is a services-layer concern)

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
        ```
    """

    def __init__(
        self,
        oauth_factory: Any | None = None,
        token_manager: Any | None = None,
        *,
        filesystem: NexusFilesystem | None = None,
        database_url: str | None = None,
        oauth_config: Any | None = None,
        mount_lister: Callable[[], list[tuple[str, str]]] | None = None,
        pkce_store: PKCEStateStore | None = None,
    ):
        """Initialize OAuth service.

        Args:
            oauth_factory: OAuthProviderFactory for creating provider instances
            token_manager: TokenManager for credential storage
            filesystem: NexusFilesystem Protocol for mkdir/write operations
            database_url: Database URL for lazy TokenManager creation
            oauth_config: OAuth provider config for lazy factory creation
            mount_lister: Callable returning (mount_point, backend_type_name) pairs
                          for connector mount discovery
            pkce_store: Optional PKCE state store (defaults to in-memory with 10min TTL)
        """
        # Compose the brick-level credential service
        self._cred = OAuthCredentialService(
            oauth_factory=oauth_factory,
            token_manager=token_manager,
            database_url=database_url,
            oauth_config=oauth_config,
            pkce_store=pkce_store,
        )

        # MCP-specific dependencies (not needed by credential service)
        self._filesystem = filesystem
        self._mount_lister = mount_lister

        # Expose internals for backward compat (tests access these directly)
        self._oauth_factory = self._cred._oauth_factory
        self._token_manager = self._cred._token_manager
        self._database_url = database_url
        self._oauth_config = oauth_config
        self._pkce_store = self._cred._pkce_store

        logger.info("[OAuthService] Initialized")

    # =========================================================================
    # Public API: Provider Discovery (delegates to brick)
    # =========================================================================

    @rpc_expose(description="List all available OAuth providers")
    async def oauth_list_providers(
        self,
        context: "OperationContext | None" = None,
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
        """
        return await self._cred.list_providers(context=context)

    # =========================================================================
    # Public API: OAuth Flow (delegates to brick)
    # =========================================================================

    @rpc_expose(description="Get OAuth authorization URL for any provider")
    async def oauth_get_auth_url(
        self,
        provider: str,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
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
        """
        return await self._cred.get_auth_url(
            provider=provider, redirect_uri=redirect_uri, scopes=scopes
        )

    @rpc_expose(description="Exchange OAuth authorization code for tokens")
    async def oauth_exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Exchange OAuth authorization code for tokens and store credentials.

        After user authorizes access, exchange the authorization code for
        access and refresh tokens. Automatically detects if provider requires
        PKCE and validates the code_verifier.

        Args:
            provider: OAuth provider name (e.g., "google", "microsoft", "x")
            code: Authorization code from OAuth callback
            user_email: User email for credential storage (optional, auto-fetched)
            state: CSRF state token from authorization request
            redirect_uri: OAuth redirect URI (must match authorization request)
            code_verifier: PKCE code verifier (required for PKCE providers)
            context: Operation context for zone isolation

        Returns:
            Dictionary containing:
                - success: Whether exchange succeeded (bool)
                - credential_id: Unique credential identifier (str)
                - user_email: User email address (str)
                - expires_at: Token expiration timestamp in ISO format (str)
                - provider: Provider name (str)
        """
        return await self._cred.exchange_code(
            provider=provider,
            code=code,
            user_email=user_email,
            state=state,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            context=context,
        )

    # =========================================================================
    # Public API: Credential Management (delegates to brick)
    # =========================================================================

    @rpc_expose(description="List all OAuth credentials")
    async def oauth_list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all OAuth credentials for the current user.

        Args:
            provider: Optional provider filter (e.g., "google")
            include_revoked: Include revoked credentials (default: False)
            context: Operation context for user/zone identification

        Returns:
            List of credential dictionaries.
        """
        return await self._cred.list_credentials(
            provider=provider, include_revoked=include_revoked, context=context
        )

    @rpc_expose(description="Revoke OAuth credential")
    async def oauth_revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Revoke an OAuth credential.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context for permission checking

        Returns:
            Dictionary containing success status.
        """
        return await self._cred.revoke_credential(
            provider=provider, user_email=user_email, context=context
        )

    @rpc_expose(description="Test OAuth credential validity")
    async def oauth_test_credential(
        self,
        provider: str,
        user_email: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Test if an OAuth credential is valid and can be refreshed.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context for permission checking

        Returns:
            Dictionary with valid, refreshed, expires_at, error fields.
        """
        return await self._cred.test_credential(
            provider=provider, user_email=user_email, context=context
        )

    # =========================================================================
    # Public API: MCP Integration (stays in services layer)
    # =========================================================================

    @rpc_expose(description="Connect to MCP provider via Klavis")
    async def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        context: "OperationContext | None" = None,
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
        import importlib as _il
        import json as json_module
        import os
        from datetime import UTC, datetime

        import httpx

        from nexus.backends.service_map import ServiceMap

        _mcp_models = _il.import_module("nexus.bricks.mcp.models")
        MCPMount = _mcp_models.MCPMount
        MCPToolConfig = _mcp_models.MCPToolConfig
        MCPToolDefinition = _mcp_models.MCPToolDefinition
        _mcp_oauth = _il.import_module("nexus.bricks.mcp.oauth_mappings")
        OAuthKlavisMappings = _mcp_oauth.OAuthKlavisMappings

        klavis_api_key = os.environ.get("KLAVIS_API_KEY")
        if not klavis_api_key:
            raise ValueError("KLAVIS_API_KEY environment variable not set")

        # Get user info from context
        user_id = "admin"
        if context:
            user_id = getattr(context, "user_id", None) or "admin"

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
                        f"Looking for credentials: oauth_provider={oauth_provider}, "
                        f"local_providers={local_providers}, user_id={user_id}"
                    )

                    # Try listing credentials to find one for this provider
                    credentials = await token_manager.list_credentials(user_id=user_id)
                    logger.info(f"Found {len(credentials)} credentials for user_id={user_id}")
                    for cred_info in credentials:
                        logger.debug(
                            f"  Credential: provider={cred_info.get('provider')}, "
                            f"user_email={cred_info.get('user_email')}, "
                            f"user_id={cred_info.get('user_id')}"
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
                                f"  Credential: provider={cred_info.get('provider')}, "
                                f"user_email={cred_info.get('user_email')}, "
                                f"user_id={cred_info.get('user_id')}"
                            )

                    for cred_info in credentials:
                        cred_provider = cred_info.get("provider")
                        if cred_provider == oauth_provider or cred_provider in local_providers:
                            user_email = cred_info.get("user_email")
                            if user_email:
                                credential = await token_manager.get_credential(
                                    provider=cred_provider,
                                    user_email=user_email,
                                )
                            if credential:
                                logger.info(
                                    f"Found matching credential for "
                                    f"{cred_provider}:{cred_info.get('user_email')}"
                                )
                                break

                    if credential and credential.access_token:
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
            if not server_url:
                server_url = status_data.get("serverUrl") or status_data.get("url")

            is_authenticated = status_data.get("isAuthenticated", False)

            # If not authenticated after passing token, return OAuth URL
            if not is_authenticated:
                oauth_url = status_data.get("oauthUrl")
                if redirect_url and oauth_url:
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

            # Step 4: Get strata URL for authenticated user
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
                        f"Klavis strata/create returned "
                        f"{strata_resp.status_code}: {strata_resp.text}"
                    )
            except Exception as e:
                logger.warning(f"Klavis strata/create failed: {e}")

            # Step 5: Get available tools
            tools: list[dict[str, Any]] = []
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
                    f"Klavis list-tools data: success={tools_data.get('success')}, "
                    f"tools_count={len(tools_data.get('tools', []))}"
                )
                if tools_data.get("success"):
                    tools = tools_data.get("tools", [])
                else:
                    logger.warning(f"Klavis list-tools returned success=False: {tools_data}")
            else:
                logger.warning(
                    f"Klavis list-tools failed: {tools_resp.status_code} - {tools_resp.text}"
                )

            # Step 6: Generate mount.json and tool files
            service_name = ServiceMap.get_service_name(mcp=provider) or provider
            mcp_base_path = f"/zone/{context.zone_id if context else 'default'}/user/{user_id}/mcp/"
            mcp_path = f"{mcp_base_path}{service_name}/"
            mount_file = f"{mcp_path}mount.json"

            service_info = ServiceMap.get_service_info(service_name)

            try:
                if self._filesystem is not None:
                    self._filesystem.sys_mkdir(mcp_path, parents=True, exist_ok=True)

                    now = datetime.now(UTC)
                    mount_config = MCPMount(
                        name=service_name,
                        description=(
                            service_info.description
                            if service_info
                            else f"{provider} MCP integration"
                        ),
                        transport="klavis_rest",
                        url=server_url or strata_url,
                        klavis_strata_id=instance_id,
                        auth_type="oauth",
                        auth_config={"klavis_user_id": klavis_user_id},
                        tools_path=mcp_path,
                        mounted=True,
                        mounted_at=now,
                        last_sync=now,
                        tool_count=len(tools),
                        tools=[t.get("name", "") for t in tools],
                        tier="user",
                    )
                    mount_json = json_module.dumps(mount_config.to_dict(), indent=2)
                    self._filesystem.sys_write(
                        mount_file, mount_json.encode("utf-8"), context=context
                    )
                    logger.info(f"Generated mount config: {mount_file}")

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
                        tool_file = f"{mcp_path}{tool_name}.json"
                        tool_json = json_module.dumps(tool_def.to_dict(), indent=2)
                        self._filesystem.sys_write(
                            tool_file,
                            tool_json.encode("utf-8"),
                            context=context,
                        )

                    logger.info(f"Generated {len(tools)} tool definitions in {mcp_path}")

            except Exception as e:
                logger.warning(f"Failed to write MCP config files: {e}")

            return {
                "provider": provider,
                "instance_id": instance_id,
                "strata_url": strata_url,
                "is_authenticated": True,
                "tools": tools,
                "tool_count": len(tools),
                "mount_path": mount_file,
                "tools_path": mcp_path,
                "user_id": klavis_user_id,
            }

    # =========================================================================
    # Backward-compat helper access (tests use these directly)
    # =========================================================================

    def _get_oauth_factory(self) -> Any:
        """Get or create OAuth provider factory (delegates to brick)."""
        return self._cred._get_oauth_factory()

    def _get_token_manager(self) -> Any:
        """Get or create TokenManager instance (delegates to brick)."""
        return self._cred._get_token_manager()

    def _map_provider_name(self, provider: str) -> str:
        """Map user-facing provider name (delegates to brick)."""
        return self._cred._map_provider_name(provider)

    def _create_provider(
        self,
        provider: str,
        redirect_uri: str | None = None,
        scopes: builtins.list[str] | None = None,
    ) -> Any:
        """Create OAuth provider instance (delegates to brick)."""
        return self._cred._create_provider(provider, redirect_uri, scopes)

    def _register_provider(self, provider_instance: Any) -> None:
        """Register provider with TokenManager (delegates to brick)."""
        self._cred._register_provider(provider_instance)

    async def _check_credential_ownership(
        self,
        provider: str,
        user_email: str,
        zone_id: str,
        context: "OperationContext | None",
        *,
        action: str = "access",
    ) -> None:
        """Verify credential ownership (delegates to brick)."""
        await self._cred._check_credential_ownership(
            provider, user_email, zone_id, context, action=action
        )

    async def _get_user_email_from_provider(
        self, provider_instance: Any, credential: Any
    ) -> str | None:
        """Get user email from provider (delegates to brick)."""
        return await self._cred._get_user_email_from_provider(provider_instance, credential)

    async def _get_authorization_url_with_pkce_support(
        self,
        provider_instance: Any,
        provider: str,
        state: str,
    ) -> dict[str, Any]:
        """Get authorization URL with PKCE (delegates to brick)."""
        return await self._cred._get_authorization_url_with_pkce_support(
            provider_instance, provider, state
        )

    async def _get_pkce_verifier(
        self,
        provider: str,
        code_verifier: str | None,
        state: str | None,
    ) -> str:
        """Get PKCE verifier (delegates to brick)."""
        return await self._cred._get_pkce_verifier(provider, code_verifier, state)


# =============================================================================
# Phase 2 Extraction: Complete ✅
# Issue #2281 / #8B: Split into brick (OAuthCredentialService) + services (OAuthService)
# =============================================================================
