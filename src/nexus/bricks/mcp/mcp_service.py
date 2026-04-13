"""MCP Service - Extracted from NexusFSMCPMixin.

This service handles all Model Context Protocol (MCP) server management operations:
- List MCP server mounts and their tools
- Mount/unmount MCP servers (stdio, SSE, Klavis transports)
- Sync/refresh tools from mounted MCP servers

Phase 2: Core Refactoring (Issue #988, Task 2.8)
Extracted from: nexus_fs_mcp.py (379 lines)
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


class MCPService:
    """Independent MCP service extracted from NexusFS.

    Handles all Model Context Protocol (MCP) server management operations:
    - List MCP mounts with filtering by tier
    - Query tools from specific MCP mounts
    - Mount MCP servers with various transports (stdio, SSE, Klavis)
    - Unmount MCP servers
    - Sync/refresh tool definitions from mounted servers

    Architecture:
        - Works with MCPMountManager for server lifecycle
        - Supports both stdio (local process) and SSE (remote HTTP) transports
        - Async operations run in separate thread to avoid event loop conflicts
        - Clean dependency injection

    Example:
        ```python
        mcp_service = MCPService(filesystem=nx)

        # List all MCP mounts
        mounts = mcp_service.mcp_list_mounts(context=context)
        for m in mounts:
            print(f"{m['name']}: {m['tool_count']} tools")

        # Mount a local MCP server
        result = mcp_service.mcp_mount(
            name="github",
            command="npx -y @modelcontextprotocol/server-github",
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"},
            context=context
        )

        # List tools from mounted server
        tools = mcp_service.mcp_list_tools("github", context=context)

        # Sync/refresh tools
        result = mcp_service.mcp_sync("github", context=context)
        print(f"Synced {result['tool_count']} tools")

        # Unmount server
        result = mcp_service.mcp_unmount("github", context=context)
        ```
    """

    def __init__(
        self,
        filesystem: Any | None = None,
        *,
        credential_service: Any = None,
        mount_lister: Callable[[], list[tuple[str, str]]] | None = None,
    ):
        """Initialize MCP service.

        Args:
            filesystem: Filesystem Protocol for list/read operations and manager creation
            credential_service: OAuthCredentialService for token lookup (mcp_connect)
            mount_lister: Callable returning (mount_point, backend_type) pairs
        """
        self._filesystem = filesystem
        self._credential_service = credential_service
        self._mount_lister = mount_lister

        logger.info("[MCPService] Initialized")

    # =========================================================================
    # Public API: MCP Mount Management
    # =========================================================================

    @rpc_expose(description="List MCP server mounts")
    async def mcp_list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """List MCP server mounts.

        Returns information about all configured MCP servers, including
        their mount status, transport type, and tool counts.

        Args:
            tier: Filter by tier (user/zone/system). None for all tiers.
            include_unmounted: Include unmounted configurations (default: True)
            context: Operation context for permission checks

        Returns:
            List of MCP mount info dicts with:
                - name: Mount name (str)
                - description: Mount description (str)
                - transport: Transport type - stdio/sse/klavis (str)
                - mounted: Whether currently mounted (bool)
                - tool_count: Number of discovered tools (int)
                - last_sync: Last sync timestamp in ISO format (str|None)
                - tools_path: Path to tools directory (str)

        Examples:
            # List all MCP mounts
            mounts = service.mcp_list_mounts(context=context)
            for m in mounts:
                print(f"{m['name']}: {m['tool_count']} tools")

            # List only system-tier mounts
            system_mounts = service.mcp_list_mounts(tier="system", context=context)

            # List only mounted servers
            active = service.mcp_list_mounts(
                include_unmounted=False,
                context=context
            )
        """
        # Get MCP mount manager
        manager = self._get_mcp_mount_manager()

        # list_mounts is async — await it directly
        mounts = await manager.list_mounts(
            include_unmounted=include_unmounted,
            tier=tier,
            context=context,
        )

        # Format mount info
        return [
            {
                "name": m.name,
                "description": m.description,
                "transport": m.transport,
                "mounted": m.mounted,
                "tool_count": m.tool_count,
                "last_sync": m.last_sync.isoformat() if m.last_sync else None,
                "tools_path": m.tools_path,
            }
            for m in mounts
        ]

    @rpc_expose(description="List tools from MCP mount")
    async def mcp_list_tools(
        self,
        name: str,
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """List tools from a specific MCP mount.

        Reads the tool definitions from the mounted MCP server's tool directory
        and returns their metadata.

        Args:
            name: MCP mount name (from mcp_list_mounts)
            context: Operation context for permission checks

        Returns:
            List of tool info dicts with:
                - name: Tool name (str)
                - description: Tool description (str)
                - input_schema: JSON schema for tool input (dict)

        Raises:
            ValidationError: If mount not found

        Examples:
            # List tools from GitHub MCP server
            tools = service.mcp_list_tools("github", context=context)
            for t in tools:
                print(f"{t['name']}: {t['description']}")

            # Check if specific tool exists
            tools = service.mcp_list_tools("github", context=context)
            has_issues = any(t['name'] == 'create_issue' for t in tools)
        """
        import asyncio
        import json

        from nexus.contracts.exceptions import ValidationError

        # Get MCP mount manager
        manager = self._get_mcp_mount_manager()

        # Get mount info (run in thread to avoid blocking)
        mount = await asyncio.to_thread(manager.get_mount, name, context=context)

        if not mount:
            raise ValidationError(f"MCP mount not found: {name}")

        # Get tools from mount config or read from filesystem
        tools = []
        if mount.tools_path:
            try:
                # List files in tools directory (run in thread)
                if self._filesystem is None:
                    raise RuntimeError("Filesystem not configured for MCPService")

                items = self._filesystem.sys_readdir(mount.tools_path, recursive=False)

                for item in items:
                    if isinstance(item, str) and item.endswith(".json"):
                        # Skip mount.json
                        if item.endswith("mount.json"):
                            continue
                        try:
                            # Read tool definition file
                            raw = self._filesystem.sys_read(item)
                            if isinstance(raw, bytes):
                                text = raw.decode("utf-8")
                            elif isinstance(raw, str):
                                text = raw
                            else:
                                continue  # dict metadata — skip
                            tool_def = json.loads(text)
                            tools.append(
                                {
                                    "name": tool_def.get("name", ""),
                                    "description": tool_def.get("description", ""),
                                    "input_schema": tool_def.get("input_schema", {}),
                                }
                            )
                        except Exception:
                            logger.debug(
                                "Skipping invalid tool definition: %s", item, exc_info=True
                            )
                            continue
            except Exception:
                logger.warning(
                    "Failed to read tools directory: %s", mount.tools_path, exc_info=True
                )

        return tools

    @rpc_expose(description="Mount MCP server")
    async def mcp_mount(
        self,
        name: str,
        transport: str | None = None,
        command: str | None = None,
        url: str | None = None,
        args: builtins.list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        tier: str = "system",  # noqa: ARG002
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Mount an MCP server.

        Creates a new MCP server mount with the specified configuration.
        Supports both local (stdio) and remote (SSE) transports. After mounting,
        automatically syncs tools from the server.

        Args:
            name: Mount name (unique identifier)
            transport: Transport type (stdio/sse/klavis). Auto-detected if not specified.
            command: Command to run MCP server (for stdio transport)
            url: URL of remote MCP server (for sse transport)
            args: Command arguments (for stdio transport)
            env: Environment variables (for stdio transport)
            headers: HTTP headers (for sse transport)
            description: Mount description
            tier: Target tier (user/zone/system, default: system)
            context: Operation context for permission checks

        Returns:
            Dict with mount info:
                - name: Mount name (str)
                - transport: Transport type (str)
                - mounted: Whether successfully mounted (bool)
                - tool_count: Number of tools discovered (int)

        Raises:
            ValidationError: If invalid parameters (e.g., neither command nor url provided)

        Examples:
            # Mount local MCP server via stdio
            result = service.mcp_mount(
                name="github",
                command="npx -y @modelcontextprotocol/server-github",
                env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"},
                context=context
            )
            print(f"Mounted {result['tool_count']} tools")

            # Mount remote MCP server via SSE
            result = service.mcp_mount(
                name="remote",
                url="http://localhost:2026/sse",
                headers={"Authorization": "Bearer token"},
                context=context
            )

            # Mount with explicit transport
            result = service.mcp_mount(
                name="local",
                transport="stdio",
                command="python -m my_mcp_server",
                args=["--config", "config.json"],
                context=context
            )

        Note:
            - Either command or url is required (not both)
            - Transport is auto-detected: stdio for command, sse for url
            - Tools are automatically synced after mounting
        """
        from nexus.bricks.mcp.models import MCPMount
        from nexus.contracts.exceptions import ValidationError

        # Validate: need either command or url
        if not command and not url:
            raise ValidationError("Either command or url is required")
        if command and url:
            raise ValidationError("Cannot specify both command and url")

        # Auto-detect transport
        if not transport:
            transport = "stdio" if command else "sse"

        # Parse command into command + args if needed
        parsed_command = command
        parsed_args = args or []
        if command and not args:
            parts = command.split()
            if len(parts) > 1:
                parsed_command = parts[0]
                parsed_args = parts[1:]

        # Create mount config
        mount_config = MCPMount(
            name=name,
            description=description or f"MCP server: {name}",
            transport=transport,
            command=parsed_command,
            args=parsed_args,
            url=url,
            env=env or {},
            headers=headers or {},
        )

        manager = self._get_mcp_mount_manager()

        # Mount the server (async operation)
        await manager.mount(mount_config)

        # Sync tools (async operation)
        tool_count = await manager.sync_tools(name)

        return {
            "name": name,
            "transport": transport,
            "mounted": True,
            "tool_count": tool_count,
        }

    @rpc_expose(description="Unmount MCP server")
    async def mcp_unmount(
        self,
        name: str,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Unmount an MCP server.

        Cleanly shuts down the MCP server connection and removes the mount.
        For stdio transports, terminates the subprocess. For SSE transports,
        closes the HTTP connection.

        Args:
            name: MCP mount name
            context: Operation context for permission checks

        Returns:
            Dict with:
                - success: Whether unmount succeeded (bool)
                - name: Mount name (str)

        Raises:
            ValidationError: If mount not found

        Examples:
            # Unmount a server
            result = service.mcp_unmount("github", context=context)
            if result["success"]:
                print(f"Unmounted {result['name']}")

            # Unmount all servers
            mounts = service.mcp_list_mounts(
                include_unmounted=False,
                context=context
            )
            for m in mounts:
                service.mcp_unmount(m['name'], context=context)
        """
        from nexus.contracts.exceptions import ValidationError

        manager = self._get_mcp_mount_manager()

        # Unmount the server (async operation)
        success = await manager.unmount(name)
        if not success:
            raise ValidationError(f"MCP mount not found: {name}")

        return {"success": True, "name": name}

    @rpc_expose(description="Sync tools from MCP server")
    async def mcp_sync(
        self,
        name: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Sync/refresh tools from an MCP server.

        Re-discovers available tools from the mounted MCP server
        and updates the local tool definitions. Useful when the server's
        tool list has changed.

        Args:
            name: MCP mount name
            context: Operation context for permission checks

        Returns:
            Dict with:
                - name: Mount name (str)
                - tool_count: Number of tools discovered (int)

        Raises:
            ValidationError: If mount not found or server not mounted

        Examples:
            # Sync tools after server update
            result = service.mcp_sync("github", context=context)
            print(f"Synced {result['tool_count']} tools from {result['name']}")

            # Check if tools changed
            old_tools = service.mcp_list_tools("github", context=context)
            service.mcp_sync("github", context=context)
            new_tools = service.mcp_list_tools("github", context=context)
            if len(new_tools) != len(old_tools):
                print("Tool list changed!")

        Note:
            Server must be mounted before syncing. Use mcp_mount() first
            if the server is not yet mounted.
        """
        import asyncio

        from nexus.contracts.exceptions import ValidationError

        manager = self._get_mcp_mount_manager()

        # Get mount to verify it exists (run in thread)
        mount = await asyncio.to_thread(manager.get_mount, name, context=context)
        if not mount:
            raise ValidationError(f"MCP mount not found: {name}")

        # Sync tools (async operation)
        tool_count = await manager.sync_tools(name)
        return {"name": name, "tool_count": tool_count}

    # =========================================================================
    # Public API: MCP Provider Connection (Klavis)
    # =========================================================================

    @rpc_expose(description="Connect to MCP provider via Klavis")
    async def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Connect to an MCP provider using Klavis hosted OAuth.

        Creates a Klavis MCP instance for the provider.  If OAuth tokens
        are stored for this provider, passes them to Klavis.  Otherwise
        returns an OAuth URL for authentication.

        Args:
            provider: MCP provider name (e.g. "google_drive", "gmail", "slack")
            redirect_url: OAuth redirect URL for OAuth flow
            context: Operation context for user identification

        Returns:
            Dict with provider, instance_id, oauth_url/strata_url, etc.

        Raises:
            ValueError: If KLAVIS_API_KEY not set or provider not supported
        """
        import importlib as _il
        import json as json_module
        import os
        from datetime import UTC, datetime

        import httpx

        from nexus.backends.misc.service_map import ServiceMap

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

            if oauth_provider and self._credential_service is not None:
                try:
                    token_manager = self._credential_service._get_token_manager()
                    credential = None

                    mapping = oauth_mappings.get_mapping(oauth_provider)
                    local_providers = mapping.local_providers if mapping else [oauth_provider]
                    logger.info(
                        "Looking for credentials: oauth_provider=%s, "
                        "local_providers=%s, user_id=%s",
                        oauth_provider,
                        local_providers,
                        user_id,
                    )

                    credentials = await token_manager.list_credentials(user_id=user_id)
                    logger.info("Found %d credentials for user_id=%s", len(credentials), user_id)

                    if not credentials:
                        logger.info(
                            "No credentials found with user_id filter, trying without filter"
                        )
                        credentials = await token_manager.list_credentials()
                        logger.info("Found %d total credentials", len(credentials))

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
                                    "Found matching credential for %s:%s",
                                    cred_provider,
                                    cred_info.get("user_email"),
                                )
                                break

                    if credential and credential.access_token:
                        logger.info(
                            "Passing stored %s token to Klavis for %s",
                            oauth_provider,
                            provider,
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
                            logger.info("Successfully passed token to Klavis for %s", provider)
                            used_stored_token = True
                        else:
                            logger.warning("Failed to pass token to Klavis: %s", set_auth_resp.text)
                except Exception as e:
                    logger.debug("Could not pass stored token to Klavis: %s", e)

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
            logger.info("Klavis instance created: id=%s, server_url=%s", instance_id, server_url)

            # Step 3: Get instance status to check if authenticated
            status_resp = await client.get(
                f"https://api.klavis.ai/mcp-server/instance/{instance_id}",
                headers=headers,
            )
            status_resp.raise_for_status()
            status_data = status_resp.json()
            if not server_url:
                server_url = status_data.get("serverUrl") or status_data.get("url")

            is_authenticated = status_data.get("isAuthenticated", False)

            # If not authenticated after passing token, return OAuth URL
            if not is_authenticated:
                oauth_url = status_data.get("oauthUrl")
                if redirect_url and oauth_url:
                    separator = "&" if "?" in oauth_url else "?"
                    oauth_url = f"{oauth_url}{separator}redirect_url={redirect_url}"

                logger.info("MCP OAuth URL generated for %s, user=%s", provider, klavis_user_id)
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
                        "Klavis strata/create returned %d: %s",
                        strata_resp.status_code,
                        strata_resp.text,
                    )
            except Exception as e:
                logger.warning("Klavis strata/create failed: %s", e)

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
            logger.info("Klavis list-tools response: %d", tools_resp.status_code)
            if tools_resp.status_code == 200:
                tools_data = tools_resp.json()
                if tools_data.get("success"):
                    tools = tools_data.get("tools", [])
                else:
                    logger.warning("Klavis list-tools returned success=False: %s", tools_data)
            else:
                logger.warning(
                    "Klavis list-tools failed: %d - %s",
                    tools_resp.status_code,
                    tools_resp.text,
                )

            # Step 6: Generate README.md, mount.json, and tool files
            service_name = ServiceMap.get_service_name(mcp=provider) or provider
            readme_base_path = f"/skills/users/{user_id}/"
            readme_path = f"{readme_base_path}{service_name}/"
            readme_file = f"{readme_path}README.md"
            mount_file = f"{readme_path}mount.json"

            service_info = ServiceMap.get_service_info(service_name)
            data_mount_path = readme_path
            if service_info and service_info.connector and self._mount_lister is not None:
                mount_entries = self._mount_lister()
                connector_variants = [
                    service_info.connector.lower().replace("_", ""),
                    service_info.connector.lower().replace("_connector", ""),
                    "googledrive",
                ]
                for mount_point_str, backend_type in mount_entries:
                    backend_type_lower = backend_type.lower()
                    for variant in connector_variants:
                        if variant in backend_type_lower:
                            data_mount_path = mount_point_str
                            break
                    if data_mount_path != readme_path:
                        break

            # Build simple README.md content inline (readme_doc module was removed)
            tool_lines = "\n".join(
                f"- **{t.get('name', '?')}**: {t.get('description', '')}" for t in tools
            )
            readme_md = (
                f"# {service_name}\n\nMount path: `{data_mount_path}`\n\n## Tools\n\n{tool_lines}\n"
            )

            try:
                if self._filesystem is not None:
                    self._filesystem.mkdir(readme_path, parents=True, exist_ok=True)
                    self._filesystem.write(readme_file, readme_md.encode("utf-8"), context=context)
                    logger.info("Generated MCP readme: %s", readme_file)

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
                        tools_path=readme_path,
                        mounted=True,
                        mounted_at=now,
                        last_sync=now,
                        tool_count=len(tools),
                        tools=[t.get("name", "") for t in tools],
                        tier="user",
                    )
                    mount_json = json_module.dumps(mount_config.to_dict(), indent=2)
                    self._filesystem.write(mount_file, mount_json.encode("utf-8"), context=context)
                    logger.info("Generated mount config: %s", mount_file)

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
                        tool_file = f"{readme_path}{tool_name}.json"
                        tool_json = json_module.dumps(tool_def.to_dict(), indent=2)
                        self._filesystem.write(
                            tool_file,
                            tool_json.encode("utf-8"),
                            context=context,
                        )

                    logger.info("Generated %d tool definitions in %s", len(tools), readme_path)

            except Exception as e:
                logger.warning("Failed to write readme files: %s", e)

            return {
                "provider": provider,
                "instance_id": instance_id,
                "strata_url": strata_url,
                "is_authenticated": True,
                "tools": tools,
                "tool_count": len(tools),
                "readme_path": readme_file,
                "mount_path": mount_file,
                "tools_path": readme_path,
                "user_id": klavis_user_id,
            }

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_mcp_mount_manager(self) -> Any:
        """Get or create MCPMountManager instance.

        Returns:
            MCPMountManager instance

        Raises:
            RuntimeError: If nexus_fs is not configured

        Note:
            Requires nexus_fs to be set. MCPMountManager needs NexusFS
            for filesystem operations when reading/writing tool definitions.
        """
        from nexus.bricks.mcp.mount import MCPMountManager

        if self._filesystem is None:
            raise RuntimeError("Filesystem not configured for MCPService")

        return MCPMountManager(self._filesystem)
