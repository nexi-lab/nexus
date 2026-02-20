"""ReBAC manager kernel protocol (Issue #2133).

Defines the contract for relationship-based access control management.
Existing implementation: ``nexus.rebac.manager.ReBACManager`` (sync).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - Issue #2133: Break circular runtime imports between services/ and core/
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.rebac_types import ConsistencyLevel, ConsistencyRequirement, WriteResult


@runtime_checkable
class ReBACManagerProtocol(Protocol):
    """Kernel contract for ReBAC permission management.

    Do NOT use ``isinstance()`` checks in hot paths — use structural
    typing via Protocol matching instead.
    """

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
        consistency: ConsistencyLevel | ConsistencyRequirement | None = None,
    ) -> bool: ...

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
        subject_zone_id: str | None = None,
        object_zone_id: str | None = None,
    ) -> WriteResult: ...

    def rebac_delete(self, tuple_id: str | WriteResult) -> bool: ...

    def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
        consistency: ConsistencyLevel = ...,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]: ...

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

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        **_kw: Any,
    ) -> list[dict[str, Any]]: ...

    def get_zone_revision(
        self,
        zone_id: str | None,
        conn: Any | None = None,
    ) -> int: ...

    def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None: ...

    def close(self) -> None: ...
