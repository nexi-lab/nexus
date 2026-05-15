"""Workspace manager service protocol (Issue #2133).

Service contract for workspace snapshot management.
Existing implementation: ``nexus.services.workspace.workspace_manager.WorkspaceManager`` (sync).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - Issue #2133: Break circular runtime imports between services/ and core/
    - Issue #2359: Moved from core/protocols/ to services/protocols/ (service tier)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WorkspaceManagerProtocol(Protocol):
    """Service contract for workspace snapshot management.

    Do NOT use ``isinstance()`` checks in hot paths — use structural
    typing via Protocol matching instead.
    """

    def create_snapshot(
        self,
        workspace_path: str,
        description: str | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]: ...

    def restore_snapshot(
        self,
        snapshot_id: str | None = None,
        snapshot_number: int | None = None,
        workspace_path: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]: ...

    def list_snapshots(
        self,
        workspace_path: str,
        limit: int = 100,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def diff_snapshots(
        self,
        snapshot_id_1: str,
        snapshot_id_2: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]: ...
