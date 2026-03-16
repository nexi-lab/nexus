"""MCP service protocol (Issue #988: Extract domain services).

Defines the contract for Model Context Protocol (MCP) server management operations.
Existing implementation: ``nexus.bricks.mcp.mcp_service.MCPService``.

Storage Affinity: **ObjectStore** — MCP tool definitions stored as JSON files.

References:
    - docs/design/KERNEL-ARCHITECTURE.md
    - Issue #988: Extract MCP service from NexusFS
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@runtime_checkable
class MCPProtocol(Protocol):
    """Service contract for MCP server management.

    Provides operations for managing Model Context Protocol servers:
    - List MCP server mounts and their tools
    - Mount/unmount MCP servers (stdio, SSE, Klavis transports)
    - Sync/refresh tools from mounted MCP servers
    """

    async def mcp_list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    async def mcp_list_tools(
        self,
        name: str,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    async def mcp_mount(
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
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

    async def mcp_unmount(
        self,
        name: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

    async def mcp_sync(
        self,
        name: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...
