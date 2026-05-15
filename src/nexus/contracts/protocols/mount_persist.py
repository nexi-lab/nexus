"""MountPersistService protocol (Issue #696).

Defines the contract for persisting and loading mount configurations in the DB.

Existing implementation: ``nexus.bricks.mount.mount_persist_service.MountPersistService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@runtime_checkable
class MountPersistProtocol(Protocol):
    """Service contract for mount persistence operations."""

    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        context: "OperationContext | None" = None,
    ) -> str: ...

    def load_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> str: ...

    def load_all_mounts(
        self,
    ) -> dict[str, Any]: ...

    def list_saved_mounts(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    def delete_saved_mount(self, mount_point: str) -> bool: ...
