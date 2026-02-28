"""MountCoreService protocol (Issue #696).

Defines the contract for mount lifecycle operations: add, remove, list, get mounts
and connectors.

Existing implementation: ``nexus.bricks.mount.mount_core_service.MountCoreService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@runtime_checkable
class MountCoreProtocol(Protocol):
    """Service contract for mount core operations."""

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        context: "OperationContext | None" = None,
    ) -> str: ...

    def remove_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

    def list_mounts(
        self,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    def get_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None: ...

    def has_mount(self, mount_point: str) -> bool: ...

    def list_connectors(
        self,
        category: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def delete_connector(
        self,
        mount_point: str,
        revoke_oauth: bool = False,
        provider: str | None = None,
        user_email: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...
