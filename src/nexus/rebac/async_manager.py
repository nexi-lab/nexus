"""Async ReBAC Manager — thin wrapper around sync core (Issue #1385).

Uses ``asyncio.to_thread()`` to wrap the synchronous ReBACManager, avoiding
the need for a separate async implementation. This reduces code duplication
by ~1200 LOC while maintaining the same async API.

Usage:
    from nexus.rebac.async_manager import AsyncReBACManager

    async_manager = AsyncReBACManager(sync_manager)
    result = await async_manager.rebac_check(subject, permission, obj)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.rebac.types import WriteResult

logger = logging.getLogger(__name__)


class AsyncReBACManager:
    """Async facade over the synchronous ReBACManager.

    Wraps all public methods with ``asyncio.to_thread()`` for non-blocking
    execution in async contexts (FastAPI, etc.).
    """

    def __init__(self, sync_manager: Any) -> None:
        """Initialize with a sync ReBACManager instance.

        Args:
            sync_manager: A ReBACManager (or EnhancedReBACManager) instance.
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
        return await asyncio.to_thread(
            self._sync.rebac_expand, permission, object, zone_id
        )

    # ── Bulk APIs ───────────────────────────────────────────────────

    async def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str = "default",
        use_rust: bool = True,
    ) -> list[bool]:
        """Async bulk permission check."""
        return await asyncio.to_thread(
            self._sync.rebac_check_bulk, checks, zone_id, use_rust
        )

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
        zone_id: str = "default",
    ) -> set[tuple[str, str]]:
        """Async transitive group lookup."""
        return await asyncio.to_thread(
            self._sync.get_transitive_groups, subject, zone_id
        )

    async def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None:
        """Async cache invalidation."""
        return await asyncio.to_thread(
            self._sync.invalidate_zone_graph_cache, zone_id
        )

    async def get_cache_stats(self) -> dict[str, Any]:
        """Async cache stats."""
        return await asyncio.to_thread(self._sync.get_cache_stats)

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
        return self._sync.enforce_zone_isolation


# ── Utility ─────────────────────────────────────────────────────────


def create_async_engine_from_url(
    database_url: str,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_recycle: int | None = None,
    prepared_statement_cache_size: int = 1024,
) -> Any:
    """Create async SQLAlchemy engine from database URL.

    Automatically selects the correct async driver:
    - postgresql:// -> postgresql+asyncpg://
    - sqlite:// -> sqlite+aiosqlite://

    Args:
        database_url: Standard database URL
        pool_size: Connection pool size (default: from env or 20)
        max_overflow: Max overflow connections (default: from env or 30)
        pool_recycle: Seconds before connection recycling (default: from env or 1800)
        prepared_statement_cache_size: Asyncpg statement cache size (default: 1024)

    Returns:
        AsyncEngine instance
    """
    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    # Convert to async driver URL
    if database_url.startswith("postgresql://"):
        async_url = database_url.replace("postgresql://", "postgresql+asyncpg://")
    elif database_url.startswith("sqlite://"):
        async_url = database_url.replace("sqlite://", "sqlite+aiosqlite://")
    else:
        async_url = database_url

    engine_kwargs: dict[str, Any] = {
        "echo": False,
        "pool_recycle": pool_recycle or int(os.getenv("NEXUS_DB_POOL_RECYCLE", "1800")),
    }

    if "postgresql" in async_url:
        engine_kwargs["pool_size"] = pool_size or int(os.getenv("NEXUS_DB_POOL_SIZE", "20"))
        engine_kwargs["max_overflow"] = max_overflow or int(
            os.getenv("NEXUS_DB_MAX_OVERFLOW", "30")
        )
        engine_kwargs["pool_timeout"] = int(os.getenv("NEXUS_DB_POOL_TIMEOUT", "30"))
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_use_lifo"] = True

        statement_timeout = os.getenv("NEXUS_DB_STATEMENT_TIMEOUT", "60000")
        engine_kwargs["connect_args"] = {
            "prepared_statement_cache_size": prepared_statement_cache_size,
            "server_settings": {
                "plan_cache_mode": "force_custom_plan",
                "statement_timeout": statement_timeout,
            },
        }

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Async PostgreSQL engine: pool_size=%d, max_overflow=%d",
                engine_kwargs["pool_size"],
                engine_kwargs["max_overflow"],
            )

    return create_async_engine(async_url, **engine_kwargs)
