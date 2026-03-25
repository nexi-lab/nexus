"""Metastore-first connector sync loop (Issue #3266).

Runs as a background asyncio task alongside the server. On each tick:
1. Iterates all mounted connectors with SYNC_ELIGIBLE capability
2. Tries delta sync (sync_delta) — writes metadata + content to metastore
3. Falls back to full BFS sync (sync_mount) for connectors without delta
4. Tracks per-mount health metrics for observability

Replaces the original CLI_BACKED-only loop from Issue #3148 with a
metastore-first model that works for all connector types (OAuth, CLI, etc.).

Key design decisions (Issue #3266):
    - Sync loop owns metastore orchestration; connectors are data fetchers
    - Delta sync returns full display paths via DeltaSyncResult
    - All delta items are processed (no artificial cap), using batched writes
    - Per-mount failure counters + last_successful_sync for health tracking
    - Thread pool sized for concurrent connector syncs with per-mount timeout
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from nexus.backends.connectors.cli.sync_types import DeltaItem, DeltaSyncResult, MountSyncState

logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL = 60  # seconds
DEFAULT_THREAD_POOL_SIZE = 20  # Decision #16B: sized for concurrent syncs
DEFAULT_PER_MOUNT_TIMEOUT = 120  # seconds — Decision #16B
DELTA_BATCH_WARNING_THRESHOLD = 500  # Decision #13A: log warning above this


class ConnectorSyncLoop:
    """Background sync loop for mounted connectors.

    Starts as an asyncio task. Periodically syncs all mounted connectors
    that declare SYNC_ELIGIBLE capability, writing results to the metastore
    for metastore-first listing and reads.

    Tracks per-mount health metrics (last_successful_sync, failure counts)
    for observability and fallback decisions.
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

        # Per-mount sync state for health tracking (Decision #8A)
        self._mount_states: dict[str, MountSyncState] = {}

        # Thread pool for blocking connector calls (Decision #16B)
        pool_size = int(os.getenv("NEXUS_SYNC_THREAD_POOL_SIZE", str(DEFAULT_THREAD_POOL_SIZE)))
        self._executor = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="sync")

        # Per-mount timeout (Decision #16B)
        self._per_mount_timeout = float(
            os.getenv("NEXUS_SYNC_PER_MOUNT_TIMEOUT", str(DEFAULT_PER_MOUNT_TIMEOUT))
        )

    # --- Lifecycle ---

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
        self._executor.shutdown(wait=False)
        logger.info("[CONNECTOR_SYNC] Stopped")

    # --- Health endpoint ---

    def get_sync_health(self) -> dict[str, Any]:
        """Return per-mount sync health for observability."""
        return {mp: state.to_dict() for mp, state in self._mount_states.items()}

    def get_mount_state(self, mount_point: str) -> MountSyncState | None:
        """Return sync state for a specific mount."""
        return self._mount_states.get(mount_point)

    # --- Main loop ---

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
        """Sync all mounted connectors with SYNC_ELIGIBLE capability."""
        try:
            mounts = await self._mount_service.list_mounts()
        except Exception:
            logger.warning("[CONNECTOR_SYNC] Failed to list mounts", exc_info=True)
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

            # Only sync connectors that declare SYNC_ELIGIBLE (Decision #5A)
            from nexus.contracts.capabilities import ConnectorCapability

            caps: frozenset[str] = getattr(backend, "capabilities", frozenset())
            if ConnectorCapability.SYNC_ELIGIBLE not in caps:
                continue

            # Ensure mount state exists
            if mp not in self._mount_states:
                self._mount_states[mp] = MountSyncState(mount_point=mp)
            state = self._mount_states[mp]

            # Skip if sync already in progress for this mount
            if state.sync_in_progress:
                continue
            state.sync_in_progress = True

            try:
                await asyncio.wait_for(
                    self._sync_one_mount(mp, backend, state),
                    timeout=self._per_mount_timeout,
                )
            except TimeoutError:
                error_msg = f"Sync timed out after {self._per_mount_timeout}s"
                state.record_failure(error_msg)
                logger.warning("[CONNECTOR_SYNC] %s: %s", mp, error_msg)
            except Exception as e:
                state.record_failure(str(e))
                logger.warning("[CONNECTOR_SYNC] %s: sync error: %s", mp, e, exc_info=True)
            finally:
                state.sync_in_progress = False

    async def _sync_one_mount(self, mp: str, backend: Any, state: MountSyncState) -> None:
        """Sync a single mount — try delta first, fall back to full BFS."""
        # Try delta sync first
        if hasattr(backend, "sync_delta"):
            try:
                delta = await self._run_delta_sync(mp, backend, state)
                if delta is not None:
                    return  # Delta sync handled it
            except Exception as e:
                logger.debug(
                    "[CONNECTOR_SYNC] %s: delta failed, falling back to full sync: %s", mp, e
                )

        # Full sync fallback
        try:
            result = await self._mount_service.sync_mount(mount_point=mp, recursive=True)
            scanned = result.get("files_scanned", 0)
            state.record_success(files_synced=scanned)
            if scanned > 0:
                logger.debug("[CONNECTOR_SYNC] %s: full sync scanned %d files", mp, scanned)
        except Exception as e:
            state.record_failure(str(e))
            logger.warning("[CONNECTOR_SYNC] %s: full sync failed: %s", mp, e)

    # --- Delta sync path ---

    async def _run_delta_sync(
        self, mp: str, backend: Any, state: MountSyncState
    ) -> DeltaSyncResult | None:
        """Execute delta sync and write results to metastore.

        Returns DeltaSyncResult if delta was processed, None if caller should
        fall back to full sync.
        """
        loop = asyncio.get_event_loop()
        raw_delta = await loop.run_in_executor(self._executor, backend.sync_delta)

        # Normalize to DeltaSyncResult (Decision #11A)
        delta = self._normalize_delta(raw_delta)

        if delta.full_sync_required:
            logger.info("[CONNECTOR_SYNC] %s: delta requests full sync", mp)
            return None  # Caller falls back to full BFS

        if not delta.has_changes:
            logger.debug("[CONNECTOR_SYNC] %s: no changes", mp)
            state.record_success(sync_token=delta.sync_token)
            return delta

        if delta.total_changes > DELTA_BATCH_WARNING_THRESHOLD:
            logger.warning(
                "[CONNECTOR_SYNC] %s: large delta (%d changes), processing in batches",
                mp,
                delta.total_changes,
            )

        logger.info(
            "[CONNECTOR_SYNC] %s: delta +%d -%d (token=%s)",
            mp,
            len(delta.added),
            len(delta.deleted),
            delta.sync_token,
        )

        # Write delta items to metastore (Decision #2A + #14A)
        start = time.monotonic()
        synced = await self._write_delta_to_metastore(mp, backend, delta)
        elapsed_ms = (time.monotonic() - start) * 1000

        # Notify search daemon for indexing
        await self._notify_new_files(mp, delta.added)

        state.record_success(files_synced=synced, sync_token=delta.sync_token)
        logger.info(
            "[CONNECTOR_SYNC] %s: delta sync complete (%d files, %.0fms)",
            mp,
            synced,
            elapsed_ms,
        )
        return delta

    def _normalize_delta(self, raw: Any) -> DeltaSyncResult:
        """Normalize raw sync_delta() output to DeltaSyncResult.

        Supports both the new DeltaSyncResult type and the legacy dict format
        for backward compatibility with existing connectors.
        """
        if isinstance(raw, DeltaSyncResult):
            return raw

        if not isinstance(raw, dict):
            return DeltaSyncResult(full_sync_required=True)

        # Legacy dict format: {"added": [...], "deleted": [...], "history_id": ..., "full_sync": bool}
        full_sync = raw.get("full_sync", False)
        sync_token = str(raw.get("history_id", "")) or raw.get("sync_token")

        added_raw = raw.get("added", [])
        added_items: list[DeltaItem] = []
        for item in added_raw:
            if isinstance(item, DeltaItem):
                added_items.append(item)
            elif isinstance(item, dict):
                added_items.append(
                    DeltaItem(
                        id=str(item.get("id", "")),
                        path=str(item.get("path", "")),
                        content_hash=item.get("content_hash"),
                        size=item.get("size", 0),
                    )
                )
            elif isinstance(item, str):
                # Legacy: bare ID string — path will be resolved during write
                added_items.append(DeltaItem(id=item, path=""))

        deleted = [str(d) for d in raw.get("deleted", [])]

        return DeltaSyncResult(
            added=added_items,
            deleted=deleted,
            sync_token=str(sync_token) if sync_token else None,
            full_sync_required=full_sync,
        )

    # --- Metastore write path (Decision #1B: sync loop owns this) ---

    async def _write_delta_to_metastore(self, mp: str, backend: Any, delta: DeltaSyncResult) -> int:
        """Write delta-synced items to metastore and content cache.

        Fetches content for added items, writes FileMetadata to metastore,
        and populates the content cache. Uses batched writes (Decision #14A).

        Returns number of items successfully synced.
        """
        synced = 0
        loop = asyncio.get_event_loop()

        # --- Process additions (fetch content + write metadata) ---
        if delta.added:
            # Batch fetch content via backend (in executor to avoid blocking event loop)
            items_with_content = await loop.run_in_executor(
                self._executor,
                lambda: self._fetch_delta_content(backend, delta.added),
            )

            # Batch write to metastore
            synced += await self._batch_write_metastore(mp, backend, items_with_content)

        # --- Process deletions ---
        if delta.deleted:
            await self._batch_delete_from_metastore(mp, delta.deleted)

        return synced

    def _fetch_delta_content(
        self, backend: Any, items: list[DeltaItem]
    ) -> list[tuple[DeltaItem, bytes]]:
        """Fetch content for delta items from the backend.

        Called in a thread executor. Tolerates partial failures — items
        that fail to fetch are skipped (they'll be retried next cycle).
        """
        results: list[tuple[DeltaItem, bytes]] = []
        for item in items:
            try:
                # Use backend's read_content with a synthetic context
                from nexus.contracts.types import OperationContext

                ctx = OperationContext(
                    user_id="system",
                    groups=[],
                    backend_path=item.path or item.id,
                )
                content = backend.read_content(item.id, context=ctx)
                if content:
                    results.append((item, content))
            except Exception:
                logger.debug(
                    "[CONNECTOR_SYNC] Failed to fetch content for %s (id=%s), skipping",
                    item.path,
                    item.id,
                )
                continue
        return results

    async def _batch_write_metastore(
        self,
        mp: str,
        backend: Any,
        items: list[tuple[DeltaItem, bytes]],
    ) -> int:
        """Batch-write items to metastore and content cache.

        Uses existing SyncService batch infrastructure where available,
        falls back to individual writes.
        """
        if not items:
            return 0

        synced = 0
        sync_svc = getattr(self._mount_service, "_sync_service", None)
        change_log = getattr(sync_svc, "_change_log", None) if sync_svc else None

        # Collect change log entries for batch upsert (Decision #14A)
        change_entries = []

        for item, content in items:
            try:
                import hashlib

                content_hash = hashlib.sha256(content).hexdigest()
                virtual_path = f"{mp}/{item.path}" if item.path else f"{mp}/{item.id}"

                # Write FileMetadata to metastore
                metastore = getattr(self._mount_service, "_metastore", None)
                if metastore is not None:
                    from nexus.contracts.metadata import FileMetadata

                    now = datetime.now()
                    meta = FileMetadata(
                        path=virtual_path,
                        backend_name=getattr(backend, "name", "unknown"),
                        physical_path=item.path or item.id,
                        size=len(content),
                        etag=content_hash,
                        created_at=now,
                        modified_at=now,
                        version=1,
                    )
                    try:
                        metastore.set(virtual_path, meta)
                    except Exception:
                        logger.debug("[CONNECTOR_SYNC] metastore.set failed for %s", virtual_path)

                # Write to content cache if backend supports it
                if hasattr(backend, "_has_caching") and backend._has_caching():
                    try:
                        backend._write_to_cache(
                            path=virtual_path,
                            content=content,
                            backend_version=content_hash,
                        )
                    except Exception:
                        logger.debug("[CONNECTOR_SYNC] cache write failed for %s", virtual_path)

                # Collect change log entry for batch upsert
                if change_log is not None:
                    from nexus.system_services.sync.change_log_store import ChangeLogEntry

                    change_entries.append(
                        ChangeLogEntry(
                            path=virtual_path,
                            backend_name=getattr(backend, "name", "unknown"),
                            size_bytes=len(content),
                            mtime=datetime.now(),
                            backend_version=item.content_hash or content_hash,
                            content_hash=content_hash,
                            synced_at=datetime.now(),
                        )
                    )

                synced += 1
            except Exception:
                logger.debug(
                    "[CONNECTOR_SYNC] Failed to write %s to metastore", item.path, exc_info=True
                )
                continue

        # Batch upsert change log entries (Decision #14A: reuse existing infra)
        if change_entries and change_log is not None:
            try:
                change_log.upsert_change_logs_batch(change_entries)
            except Exception:
                logger.warning(
                    "[CONNECTOR_SYNC] %s: batch change log upsert failed for %d entries",
                    mp,
                    len(change_entries),
                )

        return synced

    async def _batch_delete_from_metastore(self, mp: str, deleted_paths: list[str]) -> None:
        """Remove deleted items from metastore."""
        metastore = getattr(self._mount_service, "_metastore", None)
        if metastore is None:
            return

        for path in deleted_paths:
            virtual_path = f"{mp}/{path}" if not path.startswith(mp) else path
            try:
                metastore.delete(virtual_path)
            except Exception:
                logger.debug("[CONNECTOR_SYNC] delete failed for %s", virtual_path)

    # --- Search notification (Decision #6A: uses full display paths) ---

    async def _notify_new_files(self, mount_point: str, items: list[DeltaItem]) -> None:
        """Notify the search daemon about new files from delta sync.

        Uses full display paths from DeltaItem (Decision #6A) — no hardcoded
        INBOX path. The daemon's _index_refresh_loop reads content via sys_read
        which routes through the connector backend automatically.
        """
        search_svc = getattr(self._mount_service, "_search_service", None)
        if search_svc is None:
            return
        search_daemon = getattr(search_svc, "_search_daemon", None)
        if search_daemon is None:
            return

        notified = 0
        for item in items:
            path = f"{mount_point}/{item.path}" if item.path else f"{mount_point}/{item.id}"
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
