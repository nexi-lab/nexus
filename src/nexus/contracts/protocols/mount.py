"""Mount service protocol (Issue #1287: Extract domain services).

Defines the contract for mount lifecycle and persistence operations.
Existing implementation: ``nexus.core.nexus_fs_mounts.NexusFSMountsMixin``
delegating to ``MountService`` and ``MountPersistService``.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@runtime_checkable
class MountProtocol(Protocol):
    """Service contract for mount management operations.

    Sub-domains:
    - Core: add / remove / list / get mounts and connectors
    - Persistence: save / load / delete mount configurations in DB
    - Connector: delete_connector (bundled unmount + OAuth revoke + DB cleanup)
    """

    # ── Core ──────────────────────────────────────────────────────────────

    async def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        context: "OperationContext | None" = None,
    ) -> str: ...

    async def remove_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

    async def delete_connector(
        self,
        mount_point: str,
        revoke_oauth: bool = False,
        provider: str | None = None,
        user_email: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

    async def list_connectors(
        self,
        category: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def list_mounts(
        self,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    async def get_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None: ...

    async def has_mount(self, mount_point: str) -> bool: ...

    # ── Persistence ───────────────────────────────────────────────────────

    async def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        context: "OperationContext | None" = None,
    ) -> str: ...

    async def list_saved_mounts(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    async def load_mount(self, mount_point: str) -> str: ...

    async def delete_saved_mount(self, mount_point: str) -> bool: ...
