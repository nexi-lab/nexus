"""ReBAC Brick protocol (Issue #1385, #2359).

Defines the contract for the ReBAC brick that the kernel and services
layer uses to interact with the brick without hard-coupling to internals.

This is separate from PermissionProtocol (which defines the 6 core Zanzibar
APIs). ReBACBrickProtocol defines the brick lifecycle + extended APIs.

Issue #2359: Merged ReBACManagerProtocol (formerly core/protocols/rebac_manager.py)
into this protocol to eliminate duplication. Added: get_zone_revision(),
invalidate_zone_graph_cache(), close(), richer rebac_write/rebac_delete/rebac_check
signatures with consistency and cross-zone parameters.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.rebac_types import WriteResult


@runtime_checkable
class ReBACBrickProtocol(Protocol):
    """Brick contract for ReBAC operations (Issue #1385).

    Extends beyond the 6 core Zanzibar APIs to include lifecycle
    management, bulk operations, and brick metadata.

    Issue #2359: Merged with ReBACManagerProtocol — now includes
    zone revision, cache invalidation, and cross-zone parameters.
    """

    # ── Core Zanzibar APIs ──────────────────────────────────────────

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
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
    ) -> "WriteResult": ...

    def rebac_delete(self, tuple_id: "str | WriteResult") -> bool: ...

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[tuple[str, str]]: ...

    # ── Bulk APIs ───────────────────────────────────────────────────

    def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
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

    # ── Zone Revision & Cache ──────────────────────────────────────

    def get_zone_revision(
        self,
        zone_id: str | None,
        conn: Any | None = None,
    ) -> int: ...

    def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None: ...

    # ── Brick Lifecycle ─────────────────────────────────────────────

    def initialize(self) -> None:
        """Initialize the brick (create tables, warm caches)."""
        ...

    def shutdown(self) -> None:
        """Gracefully shut down the brick (flush caches, close connections)."""
        ...

    def close(self) -> None:
        """Close the brick (alias for shutdown in manager context)."""
        ...

    def verify_imports(self) -> dict[str, bool]:
        """Validate required and optional imports at startup."""
        ...
