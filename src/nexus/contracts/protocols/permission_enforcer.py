"""Permission enforcer service protocol (Issue #2133).

Service contract for path-level permission enforcement.
Existing implementation: ``nexus.bricks.rebac.enforcer.PermissionEnforcer`` (sync).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - Issue #2133: Break circular runtime imports between services/ and core/
    - Issue #2359: Moved from core/protocols/ to services/protocols/ (service tier)
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext, Permission


@runtime_checkable
class PermissionEnforcerProtocol(Protocol):
    """Service contract for path-level permission enforcement.

    Do NOT use ``isinstance()`` checks in hot paths — use structural
    typing via Protocol matching instead.
    """

    def check(
        self,
        path: str,
        permission: "Permission",
        context: "OperationContext",
    ) -> bool: ...

    def filter_list(
        self,
        paths: list[str],
        context: "OperationContext",
    ) -> list[str]: ...

    def has_accessible_descendants(
        self,
        prefix: str,
        context: "OperationContext",
    ) -> bool: ...

    def has_accessible_descendants_batch(
        self,
        prefixes: list[str],
        context: "OperationContext",
    ) -> dict[str, bool]: ...

    def invalidate_cache(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        zone_id: str | None = None,
    ) -> None: ...
