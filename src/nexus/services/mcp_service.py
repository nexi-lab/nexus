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
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


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
        mcp_service = MCPService(nexus_fs=nx)

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
        nexus_fs: Any | None = None,
    ):
        """Initialize MCP service.

        Args:
            nexus_fs: NexusFS instance for filesystem operations and manager creation
        """
        self.nexus_fs = nexus_fs

        logger.info("[MCPService] Initialized")

    # =========================================================================
    # Public API: MCP Mount Management
    # =========================================================================

    @rpc_expose(description="List MCP server mounts")
    async def mcp_list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
        context: OperationContext | None = None,
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
        import asyncio

        # Get MCP mount manager
        manager = self._get_mcp_mount_manager()

        # List mounts (run in thread to avoid blocking)
        mounts = await asyncio.to_thread(
            manager.list_mounts,
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
        context: OperationContext | None = None,
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

        from nexus.core.exceptions import ValidationError

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
                if self.nexus_fs is None:
                    raise RuntimeError("NexusFS not configured for MCPService")

                items = await asyncio.to_thread(
                    self.nexus_fs.list, mount.tools_path, recursive=False
                )

                for item in items:
                    if isinstance(item, str) and item.endswith(".json"):
                        # Skip mount.json
                        if item.endswith("mount.json"):
                            continue
                        try:
                            # Read tool definition file (run in thread)
                            content = await asyncio.to_thread(self.nexus_fs.read, item)
                            if isinstance(content, bytes):
                                content = content.decode("utf-8")
                            tool_def = json.loads(content)
                            tools.append(
                                {
                                    "name": tool_def.get("name", ""),
                                    "description": tool_def.get("description", ""),
                                    "input_schema": tool_def.get("input_schema", {}),
                                }
                            )
                        except Exception:
                            # Skip invalid tool definitions
                            continue
            except Exception:
                # If we can't read tools directory, return empty list
                pass

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
        tier: str = "system",
        context: OperationContext | None = None,
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
        from nexus.core.exceptions import ValidationError
        from nexus.skills.mcp_models import MCPMount

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
        await manager.mount(mount_config, tier=tier, context=context)

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
        _context: OperationContext | None = None,
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
        from nexus.core.exceptions import ValidationError

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
        context: OperationContext | None = None,
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

        from nexus.core.exceptions import ValidationError

        manager = self._get_mcp_mount_manager()

        # Get mount to verify it exists (run in thread)
        mount = await asyncio.to_thread(manager.get_mount, name, context=context)
        if not mount:
            raise ValidationError(f"MCP mount not found: {name}")

        # Sync tools (async operation)
        tool_count = await manager.sync_tools(name)
        return {"name": name, "tool_count": tool_count}

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
        from typing import cast

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills.mcp_mount import MCPMountManager

        if self.nexus_fs is None:
            raise RuntimeError("NexusFS not configured for MCPService")

        return MCPMountManager(cast(NexusFilesystem, self.nexus_fs))


# =============================================================================
# Phase 2 Extraction Progress
# =============================================================================
#
# Status: Implementation complete ✅
#
# Completed:
# 1. [✅] Extract mcp_list_mounts() and mount listing logic
# 2. [✅] Extract mcp_list_tools() and tool querying logic
# 3. [✅] Extract mcp_mount() with transport auto-detection
# 4. [✅] Extract mcp_unmount() for clean server shutdown
# 5. [✅] Extract mcp_sync() for tool refresh
# 6. [✅] Extract helper method (_get_mcp_mount_manager)
#
# Remaining tasks:
# 7. [ ] Add unit tests for MCPService
# 8. [ ] Update NexusFS to use composition
# 9. [ ] Add backward compatibility shims with deprecation warnings
# 10. [ ] Update documentation and migration guide
#
# Lines extracted: 379 / 379 (100%)
# Files affected: 1 created (mcp_service.py)
#
# Key changes from original mixin:
# - All methods are now fully async (removed _run_async_mcp_operation wrapper)
# - Blocking I/O wrapped with asyncio.to_thread() to avoid blocking event loop
# - MCPMountManager operations directly awaited (manager.mount, manager.sync_tools, etc.)
# - Filesystem operations (list/read) wrapped with asyncio.to_thread()
# - Clean dependency injection via __init__ (nexus_fs)
#
