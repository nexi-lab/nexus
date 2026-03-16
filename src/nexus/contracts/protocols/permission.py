"""Permission service protocol for Zanzibar-style authorization (Issue #1459).

Defines the contract for relationship-based access control (ReBAC).
Existing implementation: ``nexus.bricks.rebac.rebac_service.ReBACService``.

The core Zanzibar-inspired APIs:
    - check: Does subject have permission on object?
    - check_batch: Batch permission check for multiple subjects/objects
    - create: Create a relationship tuple
    - delete: Delete a relationship tuple
    - expand: Find all subjects with a given permission on an object
    - list_tuples: Query relationship tuples with filters
    - explain: Explain why a permission is granted/denied

Storage Affinity: **RecordStore** — relationship tuples stored in SQL.

References:
    - https://www.usenix.org/system/files/atc19-pang.pdf (Zanzibar paper)
    - docs/architecture/KERNEL-ARCHITECTURE.md
    - Issue #1459: Decompose ReBAC module
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PermissionProtocol(Protocol):
    """Service contract for relationship-based access control (ReBAC).

    Implements Zanzibar-inspired APIs for authorization.
    All methods are async (matching ReBACService implementation).
    """

    async def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: Any = None,
        zone_id: str | None = None,
    ) -> bool: ...

    async def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str | None = None,
    ) -> list[bool]: ...

    async def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: Any | None = None,
        zone_id: str | None = None,
        context: Any = None,
        column_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def rebac_delete(self, tuple_id: str) -> bool: ...

    async def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        _limit: int = 100,
    ) -> list[tuple[str, str]]: ...

    async def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        _zone_id: str | None = None,
        _limit: int = 100,
        _offset: int = 0,
    ) -> list[dict[str, Any]]: ...
