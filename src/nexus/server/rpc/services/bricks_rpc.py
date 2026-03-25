"""Bricks RPC Service — brick lifecycle management.

Issue #1704.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class BricksRPCService:
    """RPC surface for brick lifecycle operations."""

    def __init__(
        self,
        lifecycle_manager: Any,
        reconciler: Any | None = None,
    ) -> None:
        self._manager = lifecycle_manager
        self._reconciler = reconciler

    @rpc_expose(description="Get brick status", admin_only=True)
    async def bricks_status(self, name: str) -> dict[str, Any]:
        status = await self._manager.get_status(name)
        if status is None:
            return {"error": f"Brick '{name}' not found"}
        return {
            "name": name,
            "state": status.state,
            "healthy": status.healthy,
            "last_check": str(status.last_check) if status.last_check else None,
        }

    @rpc_expose(description="Mount a brick", admin_only=True)
    async def bricks_mount(self, name: str) -> dict[str, Any]:
        await self._manager.mount(name)
        return {"name": name, "mounted": True}

    @rpc_expose(description="Unmount a brick", admin_only=True)
    async def bricks_unmount(self, name: str) -> dict[str, Any]:
        await self._manager.unmount(name)
        return {"name": name, "unmounted": True}

    @rpc_expose(description="Reset a brick", admin_only=True)
    async def bricks_reset(self, name: str) -> dict[str, Any]:
        await self._manager.reset(name)
        return {"name": name, "reset": True}

    @rpc_expose(description="Remount a brick (unmount + mount)", admin_only=True)
    async def bricks_remount(self, name: str) -> dict[str, Any]:
        await self._manager.remount(name)
        return {"name": name, "remounted": True}

    @rpc_expose(description="Get brick drift status", admin_only=True)
    async def bricks_drift(self) -> dict[str, Any]:
        if self._reconciler is None:
            return {"error": "Reconciler not available"}
        drift: dict[str, Any] = await self._reconciler.check_drift()
        return drift
