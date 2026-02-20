"""ReBAC Brick protocol — unified (Issue #1385, #1891).

Defines the single contract for ReBAC authorization that the kernel,
services layer, and consumers use. Merges the former ``PermissionProtocol``
(async Zanzibar APIs) and ``ReBACBrickProtocol`` (lifecycle + bulk APIs)
into one protocol.

All methods are async (matching the ReBACService implementation).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReBACBrickProtocol(Protocol):
    """Unified brick contract for ReBAC operations.

    Merges core Zanzibar APIs, bulk APIs, and brick lifecycle.
    All methods are async per convention (Issue #1891).
    """

    # ── Core Zanzibar APIs ──────────────────────────────────────────

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
        limit: int = 100,
    ) -> list[tuple[str, str]]: ...

    async def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    # ── Bulk APIs ───────────────────────────────────────────────────

    async def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]: ...

    async def rebac_list_objects(
        self,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        zone_id: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]: ...

    # ── Brick Lifecycle ─────────────────────────────────────────────

    async def initialize(self) -> None:
        """Initialize the brick (create tables, warm caches)."""
        ...

    async def shutdown(self) -> None:
        """Gracefully shut down the brick (flush caches, close connections)."""
        ...

    def verify_imports(self) -> dict[str, bool]:
        """Validate required and optional imports at startup."""
        ...
