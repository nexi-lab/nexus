"""MCP server management operations for NexusFS.

This module contains MCP server management operations exposed via RPC:
- mcp_list_mounts: List all MCP server mounts
- mcp_list_tools: List tools from a specific MCP mount
- mcp_mount: Mount an MCP server
- mcp_unmount: Unmount an MCP server
- mcp_sync: Sync tools from an MCP server
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


class NexusFSMCPMixin:
    """Mixin providing MCP server management operations for NexusFS."""

    def _run_async_mcp_operation(self, coro: Any) -> Any:
        """Run an async MCP operation in the current or new event loop.

        Args:
            coro: Coroutine to run

        Returns:
            Result from the coroutine
        """
        try:
            # Try to get the current running loop
            loop = asyncio.get_running_loop()
            # If we're already in an async context, run in a new thread
            import threading

            result_holder: list[Any] = []
            exception_holder: list[Exception] = []

            def run_in_thread() -> None:
                try:
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    result = new_loop.run_until_complete(coro)
                    result_holder.append(result)
                except Exception as e:
                    exception_holder.append(e)
                finally:
                    new_loop.close()

            thread = threading.Thread(target=run_in_thread)
            thread.start()
            thread.join()

            if exception_holder:
                raise exception_holder[0]
            if result_holder:
                return result_holder[0]
            return None

        except RuntimeError:
            # No running loop - create one and run
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

    def _get_mcp_mount_manager(self) -> Any:
        """Get or create MCPMountManager instance.

        Returns:
            MCPMountManager instance
        """
        from typing import cast

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills.mcp_mount import MCPMountManager

        return MCPMountManager(cast(NexusFilesystem, self))

    @rpc_expose(description="List MCP server mounts")
    def mcp_list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
        _context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List MCP server mounts.

        Args:
            tier: Filter by tier (user/tenant/system)
            include_unmounted: Include unmounted configurations (default: True)
            _context: Operation context

        Returns:
            List of MCP mount info dicts with:
                - name: Mount name
                - description: Mount description
                - transport: Transport type (stdio/sse/klavis)
                - mounted: Whether currently mounted
                - tool_count: Number of discovered tools
                - last_sync: Last sync timestamp (ISO format)
                - tools_path: Path to tools directory

        Examples:
            >>> mounts = nx.mcp_list_mounts()
            >>> for m in mounts:
            ...     print(f"{m['name']}: {m['tool_count']} tools")
        """
        manager = self._get_mcp_mount_manager()
        mounts = manager.list_mounts(
            include_unmounted=include_unmounted,
            tier=tier,
            context=_context,
        )

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
    def mcp_list_tools(
        self,
        name: str,
        _context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List tools from a specific MCP mount.

        Args:
            name: MCP mount name (from mcp_list_mounts)
            _context: Operation context

        Returns:
            List of tool info dicts with:
                - name: Tool name
                - description: Tool description
                - input_schema: JSON schema for tool input

        Raises:
            ValidationError: If mount not found

        Examples:
            >>> tools = nx.mcp_list_tools("github")
            >>> for t in tools:
            ...     print(f"{t['name']}: {t['description']}")
        """
        import json

        from nexus.core.exceptions import ValidationError

        manager = self._get_mcp_mount_manager()
        mount = manager.get_mount(name, context=_context)

        if not mount:
            raise ValidationError(f"MCP mount not found: {name}")

        # Get tools from mount config or read from filesystem
        tools = []
        if mount.tools_path:
            try:
                items = self.list(mount.tools_path, recursive=False)  # type: ignore[attr-defined]
                for item in items:
                    if isinstance(item, str) and item.endswith(".json"):
                        # Skip mount.json
                        if item.endswith("mount.json"):
                            continue
                        try:
                            content = self.read(item)  # type: ignore[attr-defined]
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
                            continue
            except Exception:
                pass

        return tools

    @rpc_expose(description="Mount MCP server")
    def mcp_mount(
        self,
        name: str,
        transport: str | None = None,
        command: str | None = None,
        url: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        tier: str = "system",
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Mount an MCP server.

        Args:
            name: Mount name (unique identifier)
            transport: Transport type (stdio/sse/klavis). Auto-detected if not specified.
            command: Command to run MCP server (for stdio transport)
            url: URL of remote MCP server (for sse transport)
            args: Command arguments (for stdio transport)
            env: Environment variables
            headers: HTTP headers (for sse transport)
            description: Mount description
            tier: Target tier (user/tenant/system, default: system)
            _context: Operation context

        Returns:
            Dict with mount info:
                - name: Mount name
                - transport: Transport type
                - mounted: Whether successfully mounted
                - tool_count: Number of tools (after sync)

        Raises:
            ValidationError: If invalid parameters

        Examples:
            >>> # Mount local MCP server
            >>> result = nx.mcp_mount(
            ...     name="github",
            ...     command="npx -y @modelcontextprotocol/server-github",
            ...     env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"}
            ... )

            >>> # Mount remote MCP server
            >>> result = nx.mcp_mount(
            ...     name="remote",
            ...     url="http://localhost:8080/sse"
            ... )
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

        async def do_mount() -> dict[str, Any]:
            # Mount the server
            await manager.mount(mount_config, tier=tier, context=_context)

            # Sync tools
            tool_count = await manager.sync_tools(name)

            return {
                "name": name,
                "transport": transport,
                "mounted": True,
                "tool_count": tool_count,
            }

        return self._run_async_mcp_operation(do_mount())  # type: ignore[no-any-return]

    @rpc_expose(description="Unmount MCP server")
    def mcp_unmount(
        self,
        name: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Unmount an MCP server.

        Args:
            name: MCP mount name
            _context: Operation context

        Returns:
            Dict with:
                - success: Whether unmount succeeded
                - name: Mount name

        Raises:
            ValidationError: If mount not found

        Examples:
            >>> result = nx.mcp_unmount("github")
            >>> print(result["success"])
        """
        from nexus.core.exceptions import ValidationError

        manager = self._get_mcp_mount_manager()

        async def do_unmount() -> dict[str, Any]:
            success = await manager.unmount(name)
            if not success:
                raise ValidationError(f"MCP mount not found: {name}")
            return {"success": True, "name": name}

        return self._run_async_mcp_operation(do_unmount())  # type: ignore[no-any-return]

    @rpc_expose(description="Sync tools from MCP server")
    def mcp_sync(
        self,
        name: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Sync/refresh tools from an MCP server.

        Re-discovers available tools from the mounted MCP server
        and updates the local tool definitions.

        Args:
            name: MCP mount name
            _context: Operation context

        Returns:
            Dict with:
                - name: Mount name
                - tool_count: Number of tools discovered

        Raises:
            ValidationError: If mount not found

        Examples:
            >>> result = nx.mcp_sync("github")
            >>> print(f"Synced {result['tool_count']} tools")
        """
        from nexus.core.exceptions import ValidationError

        manager = self._get_mcp_mount_manager()

        async def do_sync() -> dict[str, Any]:
            mount = manager.get_mount(name, context=_context)
            if not mount:
                raise ValidationError(f"MCP mount not found: {name}")

            tool_count = await manager.sync_tools(name)
            return {"name": name, "tool_count": tool_count}

        return self._run_async_mcp_operation(do_sync())  # type: ignore[no-any-return]
