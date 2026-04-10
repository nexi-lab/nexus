"""Workspace RPC Service — replaces NexusFS workspace/memory/snapshot facades.

Consolidates workspace snapshot, workspace registry, and memory registry
operations behind ``@rpc_expose`` methods.  Wired via
``rpc_server.register_service()`` at server startup.
"""

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.rpc import rpc_expose
from nexus.contracts.types import OperationContext, parse_operation_context

if TYPE_CHECKING:
    from nexus.bricks.workspace.workspace_registry import WorkspaceRegistry
    from nexus.contracts.types import VFSOperations
    from nexus.services.workspace.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


class WorkspaceRPCService:
    """RPC surface for workspace, memory, and snapshot operations.

    Replaces the ~700 LOC facade in NexusFS (lines 2908-3614).
    Each method handles context normalisation via ``parse_operation_context()``
    and delegates to the underlying domain service.
    """

    def __init__(
        self,
        workspace_manager: "WorkspaceManager",
        workspace_registry: "WorkspaceRegistry",
        vfs: "VFSOperations",
        default_context: OperationContext,
        snapshot_service: Any | None = None,
    ) -> None:
        self._wm = workspace_manager
        self._wr = workspace_registry
        self._vfs = vfs
        self._default_ctx = default_context
        self._snapshot_service = snapshot_service

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ctx_or_default(self, context: OperationContext | dict | None) -> OperationContext:
        ctx = parse_operation_context(context)
        return ctx

    def _resolve_ids(self, ctx: OperationContext) -> tuple[str, str | None, str | None]:
        """Return (user_id, agent_id, zone_id) with fallback to defaults."""
        return (
            ctx.user_id or self._default_ctx.user_id,
            ctx.agent_id or self._default_ctx.agent_id,
            ctx.zone_id or self._default_ctx.zone_id,
        )

    def _require_workspace(self, path: str) -> None:
        if not self._wr.get_workspace(path):
            raise ValueError(f"Workspace not registered: {path}. Use register_workspace() first.")

    # ------------------------------------------------------------------
    # Workspace Snapshot Operations
    # ------------------------------------------------------------------

    @rpc_expose(description="Create workspace snapshot")
    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace."""
        if not workspace_path:
            raise ValueError("workspace_path must be provided")
        self._require_workspace(workspace_path)

        ctx = self._ctx_or_default(context)
        user_id, agent_id, zone_id = self._resolve_ids(ctx)

        return self._wm.create_snapshot(
            workspace_path=workspace_path,
            description=description,
            tags=tags,
            created_by=created_by,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

    @rpc_expose(description="Restore workspace snapshot")
    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot."""
        ctx = context if context is not None else self._default_ctx
        if not workspace_path:
            raise ValueError("workspace_path must be provided")
        self._require_workspace(workspace_path)

        return self._wm.restore_snapshot(
            workspace_path=workspace_path,
            snapshot_number=snapshot_number,
            user_id=ctx.user_id,
            agent_id=ctx.agent_id or self._default_ctx.agent_id,
            zone_id=ctx.zone_id or self._default_ctx.zone_id,
        )

    @rpc_expose(description="List workspace snapshots")
    def workspace_log(
        self,
        workspace_path: str | None = None,
        limit: int = 100,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List snapshot history for workspace."""
        ctx = self._ctx_or_default(context)
        if not workspace_path:
            raise ValueError("workspace_path must be provided")
        self._require_workspace(workspace_path)

        user_id, agent_id, zone_id = self._resolve_ids(ctx)
        return self._wm.list_snapshots(
            workspace_path=workspace_path,
            limit=limit,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

    @rpc_expose(description="Compare workspace snapshots")
    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots."""
        ctx = self._ctx_or_default(context)
        if not workspace_path:
            raise ValueError("workspace_path must be provided")
        self._require_workspace(workspace_path)

        user_id, agent_id, zone_id = self._resolve_ids(ctx)

        snapshots = self._wm.list_snapshots(
            workspace_path=workspace_path,
            limit=1000,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

        snap_1_id = None
        snap_2_id = None
        for snap in snapshots:
            if snap["snapshot_number"] == snapshot_1:
                snap_1_id = snap["snapshot_id"]
            if snap["snapshot_number"] == snapshot_2:
                snap_2_id = snap["snapshot_id"]

        if not snap_1_id:
            raise NexusFileNotFoundError(
                path=f"snapshot:{snapshot_1}",
                message=f"Snapshot #{snapshot_1} not found",
            )
        if not snap_2_id:
            raise NexusFileNotFoundError(
                path=f"snapshot:{snapshot_2}",
                message=f"Snapshot #{snapshot_2} not found",
            )

        return self._wm.diff_snapshots(
            snap_1_id,
            snap_2_id,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

    # ------------------------------------------------------------------
    # Transactional Snapshot Operations (Issue #1752)
    # ------------------------------------------------------------------

    @rpc_expose(description="Begin transactional snapshot")
    def snapshot_begin(
        self,
        agent_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        ttl_seconds: int = 3600,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Begin a transactional snapshot."""
        if self._snapshot_service is None:
            raise RuntimeError("Transactional snapshot service not available")
        ctx = context or self._default_ctx
        resolved_agent = agent_id or ctx.agent_id or "default"
        resolved_zone = zone_id or (ctx.zone_id if ctx else None) or ROOT_ZONE_ID
        import asyncio

        info = asyncio.run(
            self._snapshot_service.begin(
                zone_id=resolved_zone,
                agent_id=resolved_agent,
                description=description,
                ttl_seconds=ttl_seconds,
            )
        )
        return {"snapshot_id": info.snapshot_id}

    @rpc_expose(description="Commit transactional snapshot")
    def snapshot_commit(
        self,
        snapshot_id: str,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, str]:
        """Commit a snapshot — changes become permanent."""
        if self._snapshot_service is None:
            raise RuntimeError("Transactional snapshot service not available")
        import asyncio

        asyncio.run(self._snapshot_service.commit(snapshot_id))
        return {"status": "committed", "snapshot_id": snapshot_id}

    @rpc_expose(description="Rollback transactional snapshot")
    def snapshot_rollback(
        self,
        snapshot_id: str,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Rollback a snapshot — restore paths to pre-snapshot state."""
        if self._snapshot_service is None:
            raise RuntimeError("Transactional snapshot service not available")
        import asyncio

        info = asyncio.run(self._snapshot_service.rollback(snapshot_id))
        return {
            "snapshot_id": info.snapshot_id,
            "status": info.status,
            "agent_id": info.agent_id,
            "zone_id": info.zone_id,
        }

    # ------------------------------------------------------------------
    # Workspace Registry Management
    # ------------------------------------------------------------------

    @rpc_expose()
    def load_workspace_config(
        self,
        workspaces: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Load workspaces from configuration."""
        results = {
            "workspaces_registered": 0,
            "workspaces_skipped": 0,
        }

        if workspaces:
            for ws_config in workspaces:
                path = ws_config.get("path")
                if not path:
                    continue
                if self._wr.get_workspace(path):
                    results["workspaces_skipped"] += 1
                    continue
                self._wr.register_workspace(
                    path=path,
                    name=ws_config.get("name"),
                    description=ws_config.get("description", ""),
                    created_by=ws_config.get("created_by"),
                    metadata=ws_config.get("metadata"),
                )
                results["workspaces_registered"] += 1

        return results

    @rpc_expose()
    async def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: timedelta | None = None,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """Register a directory as a workspace."""
        _ = tags  # reserved for future use

        if context is None and hasattr(self, "_operation_context"):
            context = self._operation_context

        if not self._vfs.access(path, context=context):
            self._vfs.mkdir(path, parents=True, exist_ok=True, context=context)

        config = self._wr.register_workspace(
            path=path,
            name=name,
            description=description or "",
            created_by=created_by,
            metadata=metadata,
            context=context,
            session_id=session_id,
            ttl=ttl,
        )
        return config.to_dict()

    @rpc_expose()
    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace (does NOT delete files)."""
        return self._wr.unregister_workspace(path)

    @rpc_expose()
    def update_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Update an existing workspace configuration."""
        config = self._wr.update_workspace(path, name, description, metadata)
        return config.to_dict()

    @rpc_expose()
    def list_workspaces(self, context: Any | None = None) -> list[dict]:
        """List all registered workspaces for the current user."""
        user_id = None
        zone_id = None
        if context is not None:
            user_id = getattr(context, "user_id", None)
            zone_id = getattr(context, "zone_id", None)

        if not user_id or not zone_id:
            raise ValueError(
                "list_workspaces requires authenticated context with user_id and zone_id"
            )

        configs = self._wr.list_workspaces()
        user_prefix = f"/zone/{zone_id}/user/{user_id}/workspace/"
        configs = [c for c in configs if c.created_by == user_id or c.path.startswith(user_prefix)]
        return [c.to_dict() for c in configs]

    @rpc_expose()
    def get_workspace_info(self, path: str) -> dict | None:
        """Get information about a registered workspace."""
        config = self._wr.get_workspace(path)
        return config.to_dict() if config else None
