"""ReBAC Brick protocol (Issue #1385).

Defines the contract for the ReBAC brick that the kernel and services
layer uses to interact with the brick without hard-coupling to internals.

This is separate from PermissionProtocol (which defines the 6 core Zanzibar
APIs). ReBACBrickProtocol defines the brick lifecycle + extended APIs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from nexus.rebac.types import WriteResult


@runtime_checkable
class ReBACBrickProtocol(Protocol):
    """Brick contract for ReBAC operations (Issue #1385).

    Extends beyond the 6 core Zanzibar APIs to include lifecycle
    management, bulk operations, and brick metadata.
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
        expires_at: Any | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> WriteResult: ...

    def rebac_delete(self, tuple_id: str) -> bool: ...

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

    # ── Brick Lifecycle ─────────────────────────────────────────────

    def initialize(self) -> None:
        """Initialize the brick (create tables, warm caches)."""
        ...

    def shutdown(self) -> None:
        """Gracefully shut down the brick (flush caches, close connections)."""
        ...

    def verify_imports(self) -> dict[str, bool]:
        """Validate required and optional imports at startup."""
        ...
