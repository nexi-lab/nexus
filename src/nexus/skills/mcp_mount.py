"""MCP Mount Manager for external MCP server integration.

This module provides functionality for mounting external MCP servers
(e.g., GitHub, Slack, Notion) and discovering their tools.

Based on: https://www.anthropic.com/engineering/code-execution-with-mcp
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import ValidationError
from nexus.core.permissions import OperationContext
from nexus.skills.mcp_models import MCPMount, MCPToolConfig, MCPToolDefinition

if TYPE_CHECKING:
    from nexus.skills.protocols import NexusFilesystem

logger = logging.getLogger(__name__)


class MCPMountError(ValidationError):
    """Raised when MCP mount operations fail."""

    pass


class MCPMountManager:
    """Manager for mounting and interacting with external MCP servers.

    Features:
    - Mount external MCP servers (stdio, http, sse transports)
    - Discover tools from mounted servers via list_tools()
    - Store tool definitions in /skills/system/mcp-tools/{mount_name}/
    - Execute tools on mounted servers
    - Manage authentication (OAuth, API keys)

    Storage Structure (per-folder):
        /skills/system/mcp-tools/
        ├── github/
        │   ├── mount.json              # Connection info for this mount
        │   ├── SKILL.md                # Human-readable docs
        │   ├── search_repositories.json
        │   └── create_issue.json
        └── slack/
            ├── mount.json
            ├── SKILL.md
            └── send_message.json

    Example:
        >>> from nexus import connect
        >>> from nexus.skills.mcp_mount import MCPMountManager
        >>>
        >>> nx = connect()
        >>> manager = MCPMountManager(nx)
        >>>
        >>> # Mount GitHub MCP server
        >>> await manager.mount(MCPMount(
        ...     name="github",
        ...     description="GitHub API integration",
        ...     transport="stdio",
        ...     command="npx",
        ...     args=["-y", "@modelcontextprotocol/server-github"],
        ...     env={"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}
        ... ))
        >>>
        >>> # Sync tools (discover available tools)
        >>> await manager.sync_tools("github")
        >>>
        >>> # List mounted servers
        >>> mounts = manager.list_mounts()
    """

    # Tier priority for MCP mounts (higher = checked first, wins on name conflict)
    # MCP mounts have 3 levels: user > tenant > system (no agent level)
    TIER_PRIORITY = {
        "user": 3,
        "tenant": 2,
        "system": 1,
    }

    @staticmethod
    def get_mcp_tier_paths(context: OperationContext | None = None) -> dict[str, str]:
        """Get context-aware tier paths for MCP tool discovery.

        Structure:
            /skills/system/mcp-tools/               - System-wide MCP tools (priority 1)
            /skills/tenants/{tenant_id}/mcp-tools/  - Tenant shared MCP tools (priority 2)
            /skills/users/{user_id}/mcp-tools/      - User personal MCP tools (priority 3)

        Args:
            context: Operation context with user_id, tenant_id

        Returns:
            Dict mapping tier name to mcp-tools path (only tiers available for this context)
        """
        paths = {"system": "/skills/system/mcp-tools/"}

        if context:
            if context.tenant_id:
                paths["tenant"] = f"/skills/tenants/{context.tenant_id}/mcp-tools/"

            if context.user_id:
                paths["user"] = f"/skills/users/{context.user_id}/mcp-tools/"

        return paths

    # Legacy: Base path for MCP tools (system level only)
    # Use get_mcp_tier_paths(context) for context-aware paths
    MCP_TOOLS_PATH = "/skills/system/mcp-tools/"

    # Mount configuration filename (per-folder)
    MOUNT_CONFIG_FILENAME = "mount.json"

    # Legacy global mounts config (for migration)
    LEGACY_MOUNTS_CONFIG_PATH = "/skills/system/mcp-tools/.mounts.json"

    def __init__(self, filesystem: NexusFilesystem | None = None):
        """Initialize MCP mount manager.

        Args:
            filesystem: Optional filesystem instance (defaults to local FS)
        """
        self._filesystem = filesystem

        # Active mount configurations (keyed by name, may include tier info)
        self._mounts: dict[str, MCPMount] = {}

        # Tier index: tier -> list of mount names (for context-aware lookup)
        self._tier_index: dict[str, list[str]] = {}

        # Active MCP client connections
        self._clients: dict[str, Any] = {}

        # Load existing mount configurations (system tier only at init)
        self._load_mounts_config()

    def _load_mounts_config(self) -> None:
        """Load mount configurations from per-folder mount.json files.

        New structure: Each mount has its own folder with mount.json
        /skills/system/mcp-tools/{mount_name}/mount.json

        Also supports legacy .mounts.json for migration.
        """
        try:
            # First, try to load from per-folder mount.json files (new structure)
            loaded_from_folders = self._load_mounts_from_folders()

            # If no mounts found, try legacy .mounts.json
            if not loaded_from_folders:
                self._load_legacy_mounts_config()

        except Exception as e:
            logger.warning(f"Failed to load mount configurations: {e}")

    def _load_mounts_from_folders(self) -> bool:
        """Load mounts from per-folder mount.json files.

        Returns:
            True if any mounts were loaded
        """
        loaded_any = False

        try:
            if self._filesystem:
                # Check if base path exists
                if not self._filesystem.exists(self.MCP_TOOLS_PATH):
                    return False

                # List directories in MCP_TOOLS_PATH
                items = self._filesystem.list(self.MCP_TOOLS_PATH)
                for item in items:
                    # Skip files at root level
                    item_path = f"{self.MCP_TOOLS_PATH}{item}"
                    mount_json_path = f"{item_path}/mount.json"

                    try:
                        if self._filesystem.exists(mount_json_path):
                            raw_content = self._filesystem.read(mount_json_path)
                            content_str = (
                                raw_content.decode("utf-8")
                                if isinstance(raw_content, bytes)
                                else str(raw_content)
                            )
                            mount_data = json.loads(content_str)
                            mount = MCPMount.from_dict(mount_data)
                            mount.mounted = False
                            self._mounts[mount.name] = mount
                            loaded_any = True
                            logger.debug(f"Loaded mount config from {mount_json_path}")
                    except Exception as e:
                        logger.warning(f"Failed to load mount from {mount_json_path}: {e}")
            else:
                # Local filesystem
                base_path = Path(self.MCP_TOOLS_PATH.lstrip("/"))
                if not base_path.exists():
                    return False

                for mount_dir in base_path.iterdir():
                    if mount_dir.is_dir():
                        mount_json_file = mount_dir / "mount.json"
                        if mount_json_file.exists():
                            try:
                                mount_data = json.loads(mount_json_file.read_text())
                                mount = MCPMount.from_dict(mount_data)
                                mount.mounted = False
                                self._mounts[mount.name] = mount
                                loaded_any = True
                                logger.debug(f"Loaded mount config from {mount_json_file}")
                            except Exception as e:
                                logger.warning(f"Failed to load mount from {mount_json_file}: {e}")

        except Exception as e:
            logger.warning(f"Error scanning for mount configs: {e}")

        return loaded_any

    def _load_legacy_mounts_config(self) -> None:
        """Load from legacy .mounts.json and migrate to per-folder structure."""
        try:
            if self._filesystem:
                if self._filesystem.exists(self.LEGACY_MOUNTS_CONFIG_PATH):
                    raw_content = self._filesystem.read(self.LEGACY_MOUNTS_CONFIG_PATH)
                    content_str = (
                        raw_content.decode("utf-8")
                        if isinstance(raw_content, bytes)
                        else str(raw_content)
                    )
                    config = json.loads(content_str)
                    for mount_data in config.get("mounts", []):
                        mount = MCPMount.from_dict(mount_data)
                        mount.mounted = False
                        self._mounts[mount.name] = mount

                    # Migrate to new structure
                    if self._mounts:
                        logger.info("Migrating from legacy .mounts.json to per-folder mount.json")
                        self._save_mounts_config()
                        # Optionally delete legacy file after migration
                        # self._filesystem.delete(self.LEGACY_MOUNTS_CONFIG_PATH)
            else:
                config_path = Path(self.LEGACY_MOUNTS_CONFIG_PATH.lstrip("/"))
                if config_path.exists():
                    config = json.loads(config_path.read_text())
                    for mount_data in config.get("mounts", []):
                        mount = MCPMount.from_dict(mount_data)
                        mount.mounted = False
                        self._mounts[mount.name] = mount

                    # Migrate to new structure
                    if self._mounts:
                        logger.info("Migrating from legacy .mounts.json to per-folder mount.json")
                        self._save_mounts_config()

        except Exception as e:
            logger.warning(f"Failed to load legacy mount config: {e}")

    def _save_mounts_config(self) -> None:
        """Save mount configurations to per-folder mount.json files.

        Each mount is saved to its own folder:
        /skills/system/mcp-tools/{mount_name}/mount.json
        """
        for mount in self._mounts.values():
            self._save_mount_config(mount)

    def _save_mount_config(self, mount: MCPMount) -> None:
        """Save a single mount's configuration to its folder.

        Args:
            mount: Mount configuration to save
        """
        try:
            mount_json_path = f"{self.MCP_TOOLS_PATH}{mount.name}/{self.MOUNT_CONFIG_FILENAME}"
            content = json.dumps(mount.to_dict(), indent=2)

            if self._filesystem:
                # Ensure mount directory exists
                mount_dir = f"{self.MCP_TOOLS_PATH}{mount.name}/"
                with contextlib.suppress(Exception):
                    self._filesystem.mkdir(mount_dir, parents=True)

                self._filesystem.write(mount_json_path, content.encode("utf-8"))
            else:
                mount_path = Path(mount_json_path.lstrip("/"))
                mount_path.parent.mkdir(parents=True, exist_ok=True)
                mount_path.write_text(content)

            logger.debug(f"Saved mount config: {mount.name}")
        except Exception as e:
            logger.error(f"Failed to save mount config for {mount.name}: {e}")

    async def mount(self, mount_config: MCPMount) -> bool:
        """Mount an external MCP server.

        Steps:
        1. Validate configuration
        2. Connect to MCP server
        3. Store configuration
        4. Mark as mounted

        Args:
            mount_config: Mount configuration

        Returns:
            True if mount successful

        Raises:
            MCPMountError: If mount fails
        """
        # Validate configuration
        if not mount_config.name:
            raise MCPMountError("Mount name is required")

        if mount_config.transport not in ("stdio", "http", "sse", "klavis_rest"):
            raise MCPMountError(f"Unsupported transport: {mount_config.transport}")

        if mount_config.transport == "stdio" and not mount_config.command:
            raise MCPMountError("Command is required for stdio transport")

        if mount_config.transport in ("http", "sse", "klavis_rest") and not mount_config.url:
            raise MCPMountError("URL is required for http/sse/klavis_rest transport")

        # Set tools path
        mount_config.tools_path = f"{self.MCP_TOOLS_PATH}{mount_config.name}/"

        # Try to connect to the MCP server and sync tools
        try:
            # Update mount state
            mount_config.mounted = True
            mount_config.mounted_at = datetime.now(UTC)

            # Store configuration
            self._mounts[mount_config.name] = mount_config
            self._save_mounts_config()

            # Sync tools from the server
            await self.sync_tools(mount_config.name)

            logger.info(f"Mounted MCP server: {mount_config.name}")
            return True

        except Exception as e:
            raise MCPMountError(f"Failed to mount {mount_config.name}: {e}") from e

    async def _create_client(self, mount_config: MCPMount) -> Any:
        """Create an MCP client connection.

        Args:
            mount_config: Mount configuration

        Returns:
            MCP client instance
        """
        if mount_config.transport == "stdio":
            return await self._create_stdio_client(mount_config)
        elif mount_config.transport in ("http", "sse"):
            return await self._create_sse_client(mount_config)
        else:
            raise MCPMountError(f"Unsupported transport: {mount_config.transport}")

    async def _create_stdio_client(self, mount_config: MCPMount) -> Any:
        """Create a stdio MCP client.

        Args:
            mount_config: Mount configuration

        Returns:
            MCP client session
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise MCPMountError(
                "MCP client library not installed. Install with: pip install mcp"
            ) from e

        server_params = StdioServerParameters(
            command=mount_config.command or "",
            args=mount_config.args,
            env=mount_config.env or None,
        )

        # Create client session
        async with (
            stdio_client(server_params) as (read, write),
            ClientSession(read, write) as session,
        ):
            # Initialize the session
            await session.initialize()
            return session

    async def _create_sse_client(self, mount_config: MCPMount) -> Any:
        """Create an SSE/HTTP MCP client.

        Args:
            mount_config: Mount configuration

        Returns:
            MCP client session
        """
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as e:
            raise MCPMountError(
                "MCP client library not installed. Install with: pip install mcp"
            ) from e

        if not mount_config.url:
            raise MCPMountError("URL is required for SSE/HTTP transport")

        # Start with custom headers from mount config
        headers: dict[str, str] = dict(mount_config.headers) if mount_config.headers else {}

        # Add authentication headers if needed (these override custom headers)
        if mount_config.auth_type == "api_key" and mount_config.auth_config:
            api_key = mount_config.auth_config.get("api_key")
            header_name = mount_config.auth_config.get("header_name", "Authorization")
            if api_key:
                headers[header_name] = f"Bearer {api_key}"

        # Create SSE client session
        async with (
            sse_client(mount_config.url, headers=headers) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return session

    async def unmount(self, name: str) -> bool:
        """Unmount an MCP server.

        Args:
            name: Mount name

        Returns:
            True if unmount successful

        Raises:
            MCPMountError: If unmount fails
        """
        if name not in self._mounts:
            raise MCPMountError(f"Mount not found: {name}")

        # Close client connection if active
        if name in self._clients:
            try:
                client = self._clients[name]
                if hasattr(client, "close"):
                    await client.close()
            except Exception as e:
                logger.warning(f"Error closing client for {name}: {e}")
            finally:
                del self._clients[name]

        # Update mount state
        mount = self._mounts[name]
        mount.mounted = False

        self._save_mounts_config()

        logger.info(f"Unmounted MCP server: {name}")
        return True

    async def sync_tools(self, name: str) -> int:
        """Sync tools from a mounted MCP server.

        Discovers available tools via list_tools() and stores
        tool definitions in the skills filesystem.

        Args:
            name: Mount name

        Returns:
            Number of tools discovered

        Raises:
            MCPMountError: If sync fails
        """
        if name not in self._mounts:
            raise MCPMountError(f"Mount not found: {name}")

        mount = self._mounts[name]

        # Get tools from MCP server
        try:
            tools = await self._list_tools_from_server(mount)
        except Exception as e:
            raise MCPMountError(f"Failed to list tools from {name}: {e}") from e

        # Create tool definitions and store in skills filesystem
        tool_count = 0
        tool_names = []
        tool_defs = []

        for tool in tools:
            try:
                tool_def = self._create_tool_definition(tool, mount)
                await self._store_tool_definition(tool_def, mount)
                tool_count += 1
                tool_names.append(tool_def.name)
                tool_defs.append(tool_def)
            except Exception as e:
                logger.warning(f"Failed to store tool {tool.get('name')}: {e}")

        # Store single SKILL.md for the mount
        if tool_defs:
            await self._store_mount_skill_md(mount, tool_defs)

        # Update mount state
        mount.last_sync = datetime.now(UTC)
        mount.tool_count = tool_count
        mount.tools = tool_names
        self._save_mounts_config()

        logger.info(f"Synced {tool_count} tools from {name}")
        return tool_count

    async def _list_tools_from_server(self, mount: MCPMount) -> list[dict[str, Any]]:
        """List tools from an MCP server.

        Args:
            mount: Mount configuration

        Returns:
            List of tool definitions from the server
        """
        # If we have an active client, use it
        if mount.name in self._clients:
            client = self._clients[mount.name]
            if hasattr(client, "list_tools"):
                result = await client.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                    }
                    for tool in result.tools
                ]

        # Otherwise, spawn a temporary process to list tools
        if mount.transport == "stdio":
            return await self._list_tools_stdio(mount)
        elif mount.transport in ("sse", "http"):
            return await self._list_tools_sse(mount)
        elif mount.transport == "klavis_rest":
            return await self._list_tools_klavis(mount)

        return []

    async def _list_tools_stdio(self, mount: MCPMount) -> list[dict[str, Any]]:
        """List tools using stdio transport.

        Args:
            mount: Mount configuration

        Returns:
            List of tool definitions
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise MCPMountError(
                "MCP client library not installed. Install with: pip install mcp"
            ) from e

        server_params = StdioServerParameters(
            command=mount.command or "",
            args=mount.args,
            env=mount.env or None,
        )

        tools = []

        try:
            async with (
                stdio_client(server_params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.list_tools()
                tools = [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": (tool.inputSchema if hasattr(tool, "inputSchema") else {}),
                    }
                    for tool in result.tools
                ]
        except Exception as e:
            logger.error(f"Failed to list tools via stdio: {e}")
            raise

        return tools

    async def _list_tools_sse(self, mount: MCPMount) -> list[dict[str, Any]]:
        """List tools using SSE/HTTP transport.

        Args:
            mount: Mount configuration

        Returns:
            List of tool definitions
        """
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as e:
            raise MCPMountError(
                "MCP client library not installed. Install with: pip install mcp"
            ) from e

        if not mount.url:
            raise MCPMountError("URL is required for SSE/HTTP transport")

        # Start with custom headers from mount config
        headers: dict[str, str] = dict(mount.headers) if mount.headers else {}

        # Add authentication headers if needed
        if mount.auth_type == "api_key" and mount.auth_config:
            api_key = mount.auth_config.get("api_key")
            header_name = mount.auth_config.get("header_name", "Authorization")
            if api_key:
                headers[header_name] = f"Bearer {api_key}"

        tools = []

        try:
            async with (
                sse_client(mount.url, headers=headers) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.list_tools()
                tools = [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": (tool.inputSchema if hasattr(tool, "inputSchema") else {}),
                    }
                    for tool in result.tools
                ]
        except Exception as e:
            logger.error(f"Failed to list tools via SSE: {e}")
            raise

        return tools

    async def _list_tools_klavis(self, mount: MCPMount) -> list[dict[str, Any]]:
        """List tools using Klavis REST API.

        Klavis uses a REST API instead of direct MCP SSE connection.
        Tools are listed via POST /mcp-server/list-tools.

        Args:
            mount: Mount configuration with klavis_rest transport

        Returns:
            List of tool definitions
        """
        try:
            import httpx
        except ImportError as e:
            raise MCPMountError(
                "httpx library not installed. Install with: pip install httpx"
            ) from e

        if not mount.url:
            raise MCPMountError("URL (Strata server URL) is required for Klavis transport")

        # Get API key from mount config or environment
        import os

        api_key = mount.klavis_api_key or os.getenv("KLAVIS_API_KEY")
        if not api_key:
            raise MCPMountError(
                "Klavis API key not configured. Set klavis_api_key in mount config "
                "or KLAVIS_API_KEY environment variable."
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        tools = []

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.klavis.ai/mcp-server/list-tools",
                    json={"serverUrl": mount.url},
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

                if not data.get("success"):
                    raise MCPMountError(f"Klavis API error: {data.get('error')}")

                tools = [
                    {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "inputSchema": tool.get("inputSchema", {}),
                    }
                    for tool in data.get("tools", [])
                ]

        except httpx.HTTPStatusError as e:
            logger.error(f"Klavis API HTTP error: {e}")
            raise MCPMountError(f"Klavis API error: {e}") from e
        except Exception as e:
            logger.error(f"Failed to list tools via Klavis: {e}")
            raise

        return tools

    async def call_tool_klavis(
        self,
        mount: MCPMount,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a tool via Klavis REST API.

        This is the unified method for calling tools on Klavis-hosted MCP servers.

        Args:
            mount: Mount configuration with klavis_rest transport
            tool_name: Name of the tool to call
            tool_args: Arguments to pass to the tool

        Returns:
            Tool execution result

        Raises:
            MCPMountError: If tool call fails
        """
        try:
            import httpx
        except ImportError as e:
            raise MCPMountError(
                "httpx library not installed. Install with: pip install httpx"
            ) from e

        if not mount.url:
            raise MCPMountError("URL (Strata server URL) is required for Klavis transport")

        # Get API key from mount config or environment
        import os

        api_key = mount.klavis_api_key or os.getenv("KLAVIS_API_KEY")
        if not api_key:
            raise MCPMountError("Klavis API key not configured")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.klavis.ai/mcp-server/call-tool",
                    json={
                        "serverUrl": mount.url,
                        "toolName": tool_name,
                        "toolArgs": tool_args,
                        "connectionType": mount.klavis_connection_type or "StreamableHttp",
                    },
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

                if not data.get("success"):
                    raise MCPMountError(f"Klavis tool call failed: {data.get('error')}")

                call_result: dict[str, Any] = data.get("result", {})
                return call_result

        except httpx.HTTPStatusError as e:
            logger.error(f"Klavis API HTTP error: {e}")
            raise MCPMountError(f"Klavis API error: {e}") from e
        except Exception as e:
            logger.error(f"Failed to call tool via Klavis: {e}")
            raise MCPMountError(f"Klavis tool call failed: {e}") from e

    def _create_tool_definition(
        self, tool_data: dict[str, Any], mount: MCPMount
    ) -> MCPToolDefinition:
        """Create a tool definition from MCP tool data.

        Args:
            tool_data: Tool data from MCP server
            mount: Mount configuration

        Returns:
            MCPToolDefinition instance
        """
        name = tool_data.get("name", "")
        description = tool_data.get("description", "")
        input_schema = tool_data.get("inputSchema", {})

        # Create MCP config
        mcp_config = MCPToolConfig(
            endpoint=f"mcp://{mount.name}/{name}",
            input_schema=input_schema,
            requires_mount=True,
            mount_name=mount.name,
            when_to_use=description,
        )

        now = datetime.now(UTC)

        return MCPToolDefinition(
            name=name,
            description=description,
            version="1.0.0",
            skill_type="mcp_tool",
            mcp_config=mcp_config,
            created_at=now,
            modified_at=now,
        )

    async def _store_tool_definition(self, tool_def: MCPToolDefinition, mount: MCPMount) -> str:
        """Store a tool definition in the skills filesystem.

        Stores tool.json directly in the mount's tools directory.

        Args:
            tool_def: Tool definition
            mount: Mount configuration

        Returns:
            Path to stored tool
        """
        # Store tool.json directly: /skills/system/mcp-tools/github/search_repositories.json
        tool_json_path = f"{mount.tools_path}{tool_def.name}.json"

        # Create tool.json
        tool_json = json.dumps(tool_def.to_dict(), indent=2)

        if self._filesystem:
            # Ensure directory exists
            if mount.tools_path:
                with contextlib.suppress(Exception):
                    self._filesystem.mkdir(mount.tools_path, parents=True)

            # Write tool.json
            self._filesystem.write(tool_json_path, tool_json.encode("utf-8"))
        else:
            # Local filesystem
            if mount.tools_path:
                tools_dir = Path(mount.tools_path.lstrip("/"))
                tools_dir.mkdir(parents=True, exist_ok=True)
                (tools_dir / f"{tool_def.name}.json").write_text(tool_json)

        logger.debug(f"Stored tool definition: {tool_def.name}")
        return tool_json_path

    async def _store_mount_skill_md(
        self, mount: MCPMount, tool_defs: list[MCPToolDefinition]
    ) -> str:
        """Store a single SKILL.md for the mount describing all tools.

        Args:
            mount: Mount configuration
            tool_defs: List of tool definitions

        Returns:
            Path to SKILL.md
        """
        skill_md_path = f"{mount.tools_path}SKILL.md"
        skill_md = self._generate_mount_skill_md(mount, tool_defs)

        if self._filesystem:
            if mount.tools_path:
                with contextlib.suppress(Exception):
                    self._filesystem.mkdir(mount.tools_path, parents=True)
            self._filesystem.write(skill_md_path, skill_md.encode("utf-8"))
        else:
            if mount.tools_path:
                tools_dir = Path(mount.tools_path.lstrip("/"))
                tools_dir.mkdir(parents=True, exist_ok=True)
                (tools_dir / "SKILL.md").write_text(skill_md)

        logger.debug(f"Stored mount SKILL.md: {mount.name}")
        return skill_md_path

    def _generate_mount_skill_md(self, mount: MCPMount, tool_defs: list[MCPToolDefinition]) -> str:
        """Generate SKILL.md content for a mount with all its tools.

        Args:
            mount: Mount configuration
            tool_defs: List of tool definitions

        Returns:
            SKILL.md content
        """
        import yaml

        # Build frontmatter
        frontmatter: dict[str, Any] = {
            "name": mount.name,
            "description": mount.description,
            "version": "1.0.0",
            "skill_type": "mcp_tools",
            "tool_count": len(tool_defs),
            "transport": mount.transport,
        }

        if mount.command:
            frontmatter["command"] = mount.command
        if mount.url:
            frontmatter["url"] = mount.url

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)

        # Build markdown content
        content_parts = [
            f"# {mount.name}",
            "",
            mount.description,
            "",
            f"**Tools:** {len(tool_defs)}",
            f"**Transport:** {mount.transport}",
            "",
            "## Tools",
            "",
        ]

        # Add each tool
        for tool_def in sorted(tool_defs, key=lambda t: t.name):
            content_parts.append(f"### {tool_def.name}")
            content_parts.append("")
            content_parts.append(tool_def.description)
            content_parts.append("")

            # Add input parameters summary
            if tool_def.mcp_config and tool_def.mcp_config.input_schema:
                schema = tool_def.mcp_config.input_schema
                props = schema.get("properties", {})
                required = schema.get("required", [])

                if props:
                    content_parts.append("**Parameters:**")
                    for param_name, param_info in props.items():
                        param_type = param_info.get("type", "any")
                        param_desc = param_info.get("description", "")
                        req_mark = " *(required)*" if param_name in required else ""
                        content_parts.append(
                            f"- `{param_name}` ({param_type}){req_mark}: {param_desc}"
                        )
                    content_parts.append("")

        content = "\n".join(content_parts)

        return f"---\n{frontmatter_yaml}---\n\n{content}"

    async def execute_tool(self, mount_name: str, tool_name: str, args: dict[str, Any]) -> Any:
        """Execute a tool from a mounted MCP server.

        Automatically routes to the correct transport:
        - stdio/sse/http: Use MCP client
        - klavis_rest: Use Klavis REST API

        Args:
            mount_name: Mount name
            tool_name: Tool name
            args: Tool arguments

        Returns:
            Tool execution result

        Raises:
            MCPMountError: If execution fails
        """
        if mount_name not in self._mounts:
            raise MCPMountError(f"Mount not found: {mount_name}")

        mount = self._mounts[mount_name]

        # For Klavis REST transport, use the Klavis API directly
        if mount.transport == "klavis_rest":
            return await self.call_tool_klavis(mount, tool_name, args)

        # For standard MCP transports, require active client
        if not mount.mounted:
            raise MCPMountError(f"Mount {mount_name} is not active. Call mount() first.")

        if mount_name not in self._clients:
            raise MCPMountError(f"No active client for {mount_name}")

        client = self._clients[mount_name]

        try:
            result = await client.call_tool(tool_name, args)
            return result
        except Exception as e:
            raise MCPMountError(f"Tool execution failed: {e}") from e

    def discover_mounts(self, context: OperationContext | None = None) -> int:
        """Discover mounts from context-aware tier paths.

        Scans all available tiers for the given context and loads mount configurations.
        Uses tier priority: user (3) > tenant (2) > system (1).
        When same mount name exists at multiple tiers, higher priority wins.

        Args:
            context: Operation context with user_id, tenant_id

        Returns:
            Number of mounts discovered
        """
        tier_paths = self.get_mcp_tier_paths(context)
        discovered_count = 0

        # Clear existing index (but keep _mounts for active connections)
        self._tier_index = {}

        # Track which names we've seen at which priority
        seen_names: dict[str, int] = {}  # name -> priority

        # Discover from each tier (in priority order - higher first)
        for tier in sorted(
            tier_paths.keys(), key=lambda t: self.TIER_PRIORITY.get(t, 0), reverse=True
        ):
            tier_path = tier_paths[tier]
            count = self._discover_mounts_from_tier(tier, tier_path, seen_names)
            discovered_count += count

        logger.info(f"Discovered {discovered_count} mounts from {len(tier_paths)} tiers")
        return discovered_count

    def _discover_mounts_from_tier(
        self, tier: str, tier_path: str, seen_names: dict[str, int]
    ) -> int:
        """Discover mounts from a single tier path.

        Args:
            tier: Tier name (user, tenant, system)
            tier_path: Path to mcp-tools directory for this tier
            seen_names: Dict of mount names already seen with their priorities

        Returns:
            Number of mounts discovered from this tier
        """
        count = 0
        tier_priority = self.TIER_PRIORITY.get(tier, 0)

        if tier not in self._tier_index:
            self._tier_index[tier] = []

        try:
            if self._filesystem:
                # Check if path exists
                if not self._filesystem.exists(tier_path):
                    logger.debug(f"MCP tier path does not exist: {tier_path}")
                    return 0

                # List directories in tier_path
                items = self._filesystem.list(tier_path)
                for item in items:
                    item_path = f"{tier_path}{item}"
                    mount_json_path = f"{item_path}/mount.json"

                    try:
                        if self._filesystem.exists(mount_json_path):
                            raw_content = self._filesystem.read(mount_json_path)
                            content_str = (
                                raw_content.decode("utf-8")
                                if isinstance(raw_content, bytes)
                                else str(raw_content)
                            )
                            mount_data = json.loads(content_str)
                            mount = MCPMount.from_dict(mount_data)
                            mount.mounted = False

                            # Check if we've seen this name at a higher priority
                            if mount.name in seen_names:
                                existing_priority = seen_names[mount.name]
                                if tier_priority <= existing_priority:
                                    logger.debug(
                                        f"Skipping mount '{mount.name}' from {tier} "
                                        f"(already loaded from higher priority tier)"
                                    )
                                    continue

                            # Store mount with tier info
                            mount.tier = tier
                            self._mounts[mount.name] = mount
                            self._tier_index[tier].append(mount.name)
                            seen_names[mount.name] = tier_priority
                            count += 1
                            logger.debug(
                                f"Discovered mount '{mount.name}' from {tier}: {mount_json_path}"
                            )

                    except Exception as e:
                        logger.warning(f"Failed to load mount from {mount_json_path}: {e}")

            else:
                # Local filesystem
                base_path = Path(tier_path.lstrip("/"))
                if not base_path.exists():
                    logger.debug(f"MCP tier path does not exist: {tier_path}")
                    return 0

                for mount_dir in base_path.iterdir():
                    if mount_dir.is_dir():
                        mount_json_file = mount_dir / "mount.json"
                        if mount_json_file.exists():
                            try:
                                mount_data = json.loads(mount_json_file.read_text())
                                mount = MCPMount.from_dict(mount_data)
                                mount.mounted = False

                                # Check priority
                                if mount.name in seen_names:
                                    existing_priority = seen_names[mount.name]
                                    if tier_priority <= existing_priority:
                                        logger.debug(
                                            f"Skipping mount '{mount.name}' from {tier} "
                                            f"(already loaded from higher priority tier)"
                                        )
                                        continue

                                mount.tier = tier
                                self._mounts[mount.name] = mount
                                self._tier_index[tier].append(mount.name)
                                seen_names[mount.name] = tier_priority
                                count += 1
                                logger.debug(
                                    f"Discovered mount '{mount.name}' from {tier}: {mount_json_file}"
                                )

                            except Exception as e:
                                logger.warning(f"Failed to load mount from {mount_json_file}: {e}")

        except Exception as e:
            logger.warning(f"Error scanning MCP tier {tier} at {tier_path}: {e}")

        return count

    def list_mounts(
        self,
        include_unmounted: bool = True,
        tier: str | None = None,
        context: OperationContext | None = None,
    ) -> list[MCPMount]:
        """List mount configurations.

        If context is provided, discovers mounts from context-aware paths first.
        Optionally filter by tier.

        Args:
            include_unmounted: Include unmounted configurations
            tier: Optional tier filter (user, tenant, system)
            context: Optional operation context for discovery

        Returns:
            List of MCPMount configurations
        """
        # If context provided, re-discover mounts for that context
        if context:
            self.discover_mounts(context)

        # Filter by tier if specified
        if tier:
            mount_names = self._tier_index.get(tier, [])
            mounts = [self._mounts[name] for name in mount_names if name in self._mounts]
        else:
            mounts = list(self._mounts.values())

        # Filter by mounted status
        if not include_unmounted:
            mounts = [m for m in mounts if m.mounted]

        return mounts

    def get_mount(
        self,
        name: str,
        context: OperationContext | None = None,
    ) -> MCPMount | None:
        """Get mount configuration by name.

        If context is provided, discovers mounts from context-aware paths first.
        Returns the highest priority mount matching the name.

        Args:
            name: Mount name
            context: Optional operation context for discovery

        Returns:
            MCPMount or None if not found
        """
        # If context provided, re-discover mounts for that context
        if context:
            self.discover_mounts(context)

        return self._mounts.get(name)

    def remove_mount(self, name: str) -> bool:
        """Remove a mount configuration.

        Args:
            name: Mount name

        Returns:
            True if removed
        """
        if name in self._mounts:
            # Ensure unmounted
            if name in self._clients:
                # Can't remove active mount
                return False

            del self._mounts[name]
            self._save_mounts_config()
            return True

        return False

    def add_mount_config(self, mount_config: MCPMount) -> None:
        """Add a mount configuration without connecting.

        Useful for pre-configuring mounts that will be connected later.

        Args:
            mount_config: Mount configuration
        """
        # Set tools path
        mount_config.tools_path = f"{self.MCP_TOOLS_PATH}{mount_config.name}/"
        mount_config.mounted = False

        self._mounts[mount_config.name] = mount_config
        self._save_mounts_config()

        logger.info(f"Added mount configuration: {mount_config.name}")
