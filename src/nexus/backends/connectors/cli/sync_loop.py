"""Periodic connector sync loop — pulls fresh content from mounted connectors.

Runs as a background asyncio task alongside the server. On each tick:
1. Iterates all mounted CLI connectors
2. Calls sync_delta() if available (Gmail historyId, Calendar syncToken)
3. Falls back to full sync_mount() for connectors without delta support
4. Configurable interval via NEXUS_CONNECTOR_SYNC_INTERVAL (default: 60s)

Issue #3148: auto-sync for mounted connectors.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL = 60  # seconds


class ConnectorSyncLoop:
    """Background sync loop for mounted CLI connectors.

    Starts as an asyncio task. Periodically syncs all mounted connectors
    that have changed content (via delta) or on a full scan interval.
    """

    def __init__(
        self,
        mount_service: Any,
        router: Any,
        interval: float | None = None,
    ) -> None:
        self._mount_service = mount_service
        self._router = router
        self._interval = interval or float(
            os.getenv("NEXUS_CONNECTOR_SYNC_INTERVAL", str(DEFAULT_SYNC_INTERVAL))
        )
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the sync loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[CONNECTOR_SYNC] Started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        """Stop the sync loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("[CONNECTOR_SYNC] Stopped")

    async def _loop(self) -> None:
        """Main sync loop — runs until stopped."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                await self._sync_all()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("[CONNECTOR_SYNC] Loop error", exc_info=True)

    async def _sync_all(self) -> None:
        """Sync all mounted connectors."""
        try:
            mounts = await self._mount_service.list_mounts()
        except Exception:
            return

        for mount in mounts:
            mp = mount.get("mount_point", "")
            if mp == "/":
                continue

            # Get backend
            try:
                route = self._router.route(f"{mp}/_.yaml")
                if not route:
                    continue
                backend = route.backend
            except Exception:
                continue

            # Only sync CLI-backed connectors
            from nexus.contracts.capabilities import ConnectorCapability

            caps: frozenset[str] = getattr(backend, "capabilities", frozenset())
            if ConnectorCapability.CLI_BACKED not in caps:
                continue

            # Try delta sync first
            if hasattr(backend, "sync_delta"):
                try:
                    delta = await asyncio.to_thread(backend.sync_delta)
                    added = len(delta.get("added", []))
                    deleted = len(delta.get("deleted", []))
                    if added > 0 or deleted > 0:
                        logger.info(
                            "[CONNECTOR_SYNC] %s: delta +%d -%d (hid=%s)",
                            mp,
                            added,
                            deleted,
                            delta.get("history_id"),
                        )
                        # Trigger full sync to update metadata
                        await self._mount_service.sync_mount(mount_point=mp, recursive=True)
                    else:
                        logger.debug("[CONNECTOR_SYNC] %s: no changes", mp)
                    continue
                except Exception:
                    logger.debug("[CONNECTOR_SYNC] %s: delta failed, falling back", mp)

            # Full sync for connectors without delta
            try:
                result = await self._mount_service.sync_mount(mount_point=mp, recursive=True)
                scanned = result.get("files_scanned", 0)
                if scanned > 0:
                    logger.debug("[CONNECTOR_SYNC] %s: scanned %d", mp, scanned)
            except Exception:
                logger.debug("[CONNECTOR_SYNC] %s: sync failed", mp, exc_info=True)
