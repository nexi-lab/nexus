"""Async ReBAC Manager — thin wrapper around sync core (Issue #1385).

Uses ``asyncio.to_thread()`` to wrap the synchronous ReBACManager, avoiding
the need for a separate async implementation. This reduces code duplication
by ~1200 LOC while maintaining the same async API.

Usage:
    from nexus.bricks.rebac.async_manager import AsyncReBACManager

    async_manager = AsyncReBACManager(sync_manager)
    result = await async_manager.rebac_check(subject, permission, obj)
"""

import asyncio
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.rebac_types import WriteResult


class AsyncReBACManager:
    """Async facade over the synchronous ReBACManager.

    Wraps all public methods with ``asyncio.to_thread()`` for non-blocking
    execution in async contexts (FastAPI, etc.).
    """

    def __init__(self, sync_manager: Any) -> None:
        """Initialize with a sync ReBACManager instance.

        Args:
            sync_manager: A ReBACManager (or ReBACManager) instance.
        """
        self._sync = sync_manager

    # ── Core Zanzibar APIs ──────────────────────────────────────────

    async def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
        consistency: Any | None = None,
    ) -> bool:
        """Async permission check."""
        return await asyncio.to_thread(
            self._sync.rebac_check,
            subject,
            permission,
            object,
            context,
            zone_id,
            consistency,
        )

    async def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: Any | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> WriteResult:
        """Async tuple write."""
        return await asyncio.to_thread(
            self._sync.rebac_write,
            subject,
            relation,
            object,
            expires_at,
            conditions,
            zone_id,
        )

    async def rebac_delete(self, tuple_id: str) -> bool:
        """Async tuple delete."""
        return await asyncio.to_thread(self._sync.rebac_delete, tuple_id)

    async def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """Async permission expansion."""
        return await asyncio.to_thread(self._sync.rebac_expand, permission, object, zone_id)

    # ── Bulk APIs ───────────────────────────────────────────────────

    async def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str = ROOT_ZONE_ID,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        """Async bulk permission check."""
        return await asyncio.to_thread(self._sync.rebac_check_bulk, checks, zone_id)

    async def rebac_list_objects(
        self,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        zone_id: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        """Async list objects."""
        return await asyncio.to_thread(
            self._sync.rebac_list_objects,
            subject,
            permission,
            object_type,
            zone_id,
            path_prefix,
            limit,
            offset,
        )

    async def rebac_write_batch(
        self,
        tuples: list[dict[str, Any]],
    ) -> int:
        """Async batch write."""
        return await asyncio.to_thread(self._sync.rebac_write_batch, tuples)

    async def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Async permission explanation."""
        return await asyncio.to_thread(
            self._sync.rebac_explain, subject, permission, object, zone_id
        )

    # ── Namespace APIs ──────────────────────────────────────────────

    async def get_namespace(self, object_type: str) -> Any:
        """Async namespace lookup."""
        return await asyncio.to_thread(self._sync.get_namespace, object_type)

    async def create_namespace(self, namespace: Any) -> None:
        """Async namespace creation."""
        return await asyncio.to_thread(self._sync.create_namespace, namespace)

    # ── Cache / Leopard / Tiger APIs ────────────────────────────────

    async def get_transitive_groups(
        self,
        subject: tuple[str, str],
        zone_id: str = ROOT_ZONE_ID,
    ) -> set[tuple[str, str]]:
        """Async transitive group lookup."""
        return await asyncio.to_thread(self._sync.get_transitive_groups, subject, zone_id)

    async def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None:
        """Async cache invalidation."""
        return await asyncio.to_thread(self._sync.invalidate_zone_graph_cache, zone_id)

    async def get_cache_stats(self) -> dict[str, Any]:
        """Async cache stats."""
        return await asyncio.to_thread(self._sync.get_cache_stats)

    def get_l1_cache_stats(self) -> dict[str, Any]:
        """Synchronous cache stats (alias for bridge compatibility)."""
        return self._sync.get_cache_stats()  # type: ignore[no-any-return]  # allowed

    # ── Bridge convenience methods ─────────────────────────────────

    async def write_tuple(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        subject_relation: str | None = None,  # noqa: ARG002
    ) -> str:
        """Write a relationship tuple and return the tuple_id.

        Convenience wrapper around rebac_write for AsyncReBACBridge.
        """
        result = await self.rebac_write(
            subject=subject, relation=relation, object=object, zone_id=zone_id
        )
        return result.tuple_id

    async def delete_tuple(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        """Delete a relationship tuple by its components.

        Convenience wrapper for AsyncReBACBridge that looks up the tuple_id
        by (subject, relation, object, zone_id) and deletes it.
        """

        def _delete_by_components() -> bool:
            from nexus.lib.zone import normalize_zone_id

            nz = normalize_zone_id(zone_id)
            with self._sync._connection() as conn:
                cursor = self._sync._create_cursor(conn)
                cursor.execute(
                    self._sync._fix_sql_placeholders(
                        "SELECT tuple_id FROM rebac_tuples "
                        "WHERE subject_type = ? AND subject_id = ? "
                        "AND relation = ? AND object_type = ? AND object_id = ? "
                        "AND zone_id = ?"
                    ),
                    (subject[0], subject[1], relation, object[0], object[1], nz),
                )
                row = cursor.fetchone()
            if not row:
                return False
            return self._sync.rebac_delete(row[0])  # type: ignore[no-any-return]  # allowed

        return await asyncio.to_thread(_delete_by_components)

    # ── Lifecycle ───────────────────────────────────────────────────

    async def close(self) -> None:
        """Async close."""
        return await asyncio.to_thread(self._sync.close)

    # ── Passthrough properties ──────────────────────────────────────

    @property
    def engine(self) -> Any:
        """Delegate to sync manager's engine."""
        return self._sync.engine

    @property
    def enforce_zone_isolation(self) -> bool:
        """Delegate to sync manager's zone isolation setting."""
        return self._sync.enforce_zone_isolation  # type: ignore[no-any-return]  # allowed
