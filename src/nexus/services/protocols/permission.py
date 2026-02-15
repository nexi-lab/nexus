"""Permission service protocol for Zanzibar-style authorization (Issue #1459).

Defines the contract for relationship-based access control (ReBAC).
Existing implementation: ``nexus.services.permissions.rebac_manager_enhanced.EnhancedReBACManager``.

The 6 core Zanzibar APIs:
    - check: Does subject have permission on object?
    - check_bulk: Batch permission check for multiple subjects/objects
    - write: Create a relationship tuple
    - delete: Delete a relationship tuple
    - expand: Find all subjects with a given permission on an object
    - list_objects: Find all objects a subject can access

Storage Affinity: **RecordStore** — relationship tuples stored in SQL.

References:
    - https://www.usenix.org/system/files/atc19-pang.pdf (Zanzibar paper)
    - docs/design/KERNEL-ARCHITECTURE.md
    - Issue #1459: Decompose ReBAC module
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PermissionProtocol(Protocol):
    """Service contract for relationship-based access control (ReBAC).

    Implements the 6 core Zanzibar APIs for authorization.
    All methods are synchronous (async wrappers exist separately).
    """

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool: ...

    def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]: ...

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: Any | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> Any: ...

    def rebac_delete(self, tuple_id: str) -> bool: ...

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[tuple[str, str]]: ...

    def rebac_list_objects(
        self,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        zone_id: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]: ...
