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
                    added_ids = delta.get("added", [])
                    deleted_ids = delta.get("deleted", [])
                    if added_ids or deleted_ids:
                        logger.info(
                            "[CONNECTOR_SYNC] %s: delta +%d -%d (hid=%s)",
                            mp,
                            len(added_ids),
                            len(deleted_ids),
                            delta.get("history_id"),
                        )
                        # Notify search daemon of new files so the kernel's
                        # auto-index pipeline handles them (debounced, batched,
                        # content-hash dedup). Much cheaper than full BFS re-read.
                        if added_ids:
                            await self._notify_new_files(mp, backend, added_ids)
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

    async def _notify_new_files(
        self, mount_point: str, backend: Any, message_ids: list[str]
    ) -> None:
        """Notify the search daemon about new files from delta sync.

        Uses the kernel's notify_file_change() primitive which debounces
        at 5s intervals, coalesces subtrees, and auto-indexes via the
        IndexingService pipeline (with content-hash dedup).

        This is O(delta) not O(total_files) — only notifies for new messages.
        """
        search_svc = getattr(self._mount_service, "_search_service", None)
        if search_svc is None:
            return
        search_daemon = getattr(search_svc, "_search_daemon", None)
        if search_daemon is None:
            return

        notified = 0
        for msg_id in message_ids[:50]:  # Cap at 50 per delta cycle
            # Notify for INBOX path (most common label for new messages).
            # The daemon's _index_refresh_loop will read content via sys_read
            # which routes through the connector backend automatically.
            path = f"{mount_point}/INBOX/{msg_id}-{msg_id}.yaml"
            try:
                await search_daemon.notify_file_change(path, change_type="create")
                notified += 1
            except Exception:
                continue

        if notified:
            logger.info(
                "[CONNECTOR_SYNC] Notified search daemon of %d new files in %s",
                notified,
                mount_point,
            )
