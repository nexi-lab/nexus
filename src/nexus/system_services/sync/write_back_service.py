"""Write-Back Service for bidirectional sync (Issue #1129, #1130, #3194).

Subscribes to kernel VFS mutations via KernelDispatch OBSERVE hook and writes
changes back to source backends. Handles conflict detection/resolution, retry,
and rate-limiting per backend.

Architecture (Issue #3194):
- OBSERVE hook: receives FILE_WRITE/DELETE/RENAME/DIR_CREATE/DIR_DELETE events
  directly from KernelDispatch (us latency, replaces EventBus subscription)
- DT_PIPE wakeup: backlog enqueue signals the poll loop via pipe (us wakeup,
  replaces 30s asyncio.sleep polling)
- Polling fallback: 30s safety net if pipe unavailable or signal missed
- Rate-limited: per-backend asyncio.Semaphore
- Conflict-aware: 6 configurable strategies via ConflictStrategy (Issue #1130)
"""

import asyncio
import contextlib
import dataclasses
import logging
import posixpath
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.file_events import FILE_EVENT_BIT
from nexus.system_services.event_bus.types import FileEvent, FileEventType

from .conflict_resolution import (
    ConflictAbortError,
    ConflictContext,
    ConflictRecord,
    ConflictStatus,
    ConflictStrategy,
    ResolutionOutcome,
    detect_conflict,
    resolve_conflict,
)
from .write_back_metrics import WriteBackMetrics

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.system_services.event_bus.base import EventBusBase
    from nexus.system_services.gateway import NexusFSGateway

    from .change_log_store import ChangeLogStore
    from .conflict_log_store import ConflictLogStore
    from .sync_backlog_store import SyncBacklogEntry, SyncBacklogStore

logger = logging.getLogger(__name__)

# Event types that trigger write-back
_WRITE_BACK_EVENT_TYPES = frozenset(
    {
        FileEventType.FILE_WRITE,
        FileEventType.FILE_DELETE,
        FileEventType.FILE_RENAME,
        FileEventType.DIR_CREATE,
        FileEventType.DIR_DELETE,
    }
)

# Rust-side bitmask for OBSERVE filtering (Issue #3194, #4A).
# Only mutation events — excludes SYNC_TO_BACKEND_*, CONFLICT_DETECTED
# to prevent feedback loops.
_WRITE_BACK_EVENT_MASK: int = 0
for _evt in _WRITE_BACK_EVENT_TYPES:
    _WRITE_BACK_EVENT_MASK |= FILE_EVENT_BIT.get(_evt, 0)

# DT_PIPE path for backlog wakeup signalling (Issue #3194)
_BACKLOG_WAKEUP_PIPE = "/nexus/pipes/sync-backlog-wakeup"
_BACKLOG_PIPE_CAPACITY = 256  # 256 signals, matching NOTIFY_PIPE_CAPACITY convention

# VFSSemaphore TTL for per-backend rate limiting (Issue #3194).
# If a worker crashes mid-write-back, the permit auto-expires after this duration.
# 5 minutes is generous — typical backend I/O is seconds, not minutes.
_SEMAPHORE_TTL_MS = 300_000  # 5 minutes

# Retry interval when waiting for a semaphore permit
_SEMAPHORE_RETRY_INTERVAL = 0.1  # 100ms


class WriteBackService:
    """Orchestrates bidirectional sync from Nexus to source backends.

    Implements the VFSObserver protocol (Issue #3194) to receive file mutation
    events directly from KernelDispatch OBSERVE phase, replacing the previous
    EventBus subscription loop.

    Responsibilities:
    1. Receive VFS mutation events via on_mutation() (OBSERVE hook)
    2. Enqueue events to SyncBacklogStore
    3. Process pending entries: call backend write/delete/mkdir
    4. Handle conflicts via conflict_resolution module
    5. Rate-limit per backend via asyncio.Semaphore
    """

    # ── VFSObserver protocol (Issue #3194, #4A) ──────────────────────────
    # Rust ObserverRegistry pre-filters by bitmask before calling Python.
    event_mask: int = _WRITE_BACK_EVENT_MASK

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(observers=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    async def on_mutation(self, event: "FileEvent") -> None:
        """OBSERVE-phase handler — receive FileEvent from KernelDispatch.

        Replaces _subscribe_loop() EventBus subscription (Issue #3194).
        KernelDispatch guarantees typed FileEventType (no string conversion needed).
        """
        await self._on_file_event(event)

    # ── Constructor & lifecycle ──────────────────────────────────────────

    def __init__(
        self,
        gateway: "NexusFSGateway",
        event_bus: "EventBusBase",
        backlog_store: "SyncBacklogStore",
        change_log_store: "ChangeLogStore",
        default_strategy: ConflictStrategy = ConflictStrategy.KEEP_NEWER,
        conflict_log_store: "ConflictLogStore | None" = None,
        max_concurrent_per_backend: int = 10,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 50,
        pipe_manager: Any = None,
    ) -> None:
        """Initialize WriteBackService.

        Args:
            gateway: NexusFSGateway for mount/file resolution
            event_bus: Event bus for publishing completion/failure events
            backlog_store: SyncBacklogStore for pending operations
            change_log_store: ChangeLogStore for conflict detection
            default_strategy: Global default conflict strategy
            conflict_log_store: Optional store for conflict audit logging
            max_concurrent_per_backend: Max concurrent write-backs per backend
            poll_interval_seconds: Interval between polling sweeps (safety net)
            batch_size: Max entries fetched per backend per poll cycle
            pipe_manager: Optional PipeManager for DT_PIPE wakeup (Issue #3194).
                          None = polling-only fallback.
        """
        self._gw = gateway
        self._event_bus = event_bus
        self._backlog_store = backlog_store
        self._change_log_store = change_log_store
        self._default_strategy = default_strategy
        self._conflict_log_store = conflict_log_store
        self._max_concurrent = max_concurrent_per_backend
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._pipe_manager = pipe_manager

        # Issue #3194: VFSSemaphore replaces dict[str, asyncio.Semaphore] for
        # per-backend rate limiting. Provides TTL auto-release on crash,
        # holder tracking (who's writing?), and force_release() for stuck workers.
        from nexus.lib.semaphore import create_vfs_semaphore

        self._vfs_sem = create_vfs_semaphore()
        self._metrics = WriteBackMetrics()
        # Pre-built system context template — avoids UUID generation per-operation
        self._system_ctx = OperationContext(user_id="system", groups=[], is_system=True)
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the pipe-driven poll loop.

        The OBSERVE hook (on_mutation) is registered separately via the factory
        coordinator — it does not require start() to function.
        """
        if self._running:
            return
        self._running = True

        # Create DT_PIPE for backlog wakeup (Issue #3194, best-effort)
        if self._pipe_manager is not None:
            try:
                self._pipe_manager.ensure(_BACKLOG_WAKEUP_PIPE, capacity=_BACKLOG_PIPE_CAPACITY)
                logger.debug("[WRITE_BACK] Backlog wakeup pipe created at %s", _BACKLOG_WAKEUP_PIPE)
            except Exception as e:
                logger.debug(
                    "[WRITE_BACK] Failed to create backlog pipe: %s (polling fallback active)", e
                )
                self._pipe_manager = None  # Disable pipe path; fall back to timer

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[WRITE_BACK] Service started")

    async def stop(self) -> None:
        """Gracefully shut down the service."""
        self._running = False

        # Close pipe to unblock any waiting poll loop iteration
        if self._pipe_manager is not None:
            with contextlib.suppress(Exception):
                self._pipe_manager.signal_close(_BACKLOG_WAKEUP_PIPE)

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        self._poll_task = None
        logger.info("[WRITE_BACK] Service stopped")

    # ── Event handling ───────────────────────────────────────────────────

    async def _on_file_event(self, event: FileEvent) -> None:
        """Handle an incoming file event: filter and enqueue if applicable.

        Called by on_mutation() from KernelDispatch OBSERVE phase.
        Kernel guarantees typed FileEventType — no string conversion needed (#7A).
        """
        event_type = (
            event.type if isinstance(event.type, FileEventType) else FileEventType(event.type)
        )
        if event_type not in _WRITE_BACK_EVENT_TYPES:
            return

        mount_info = self._gw.get_mount_for_path(event.path)
        if mount_info is None:
            return

        # Skip readonly mounts
        if mount_info["readonly"]:
            return

        # Skip external-content backends (e.g. LocalConnector, HN, IPC) — content is
        # managed externally, write-back would double-write or be meaningless.
        # Also skip RemoteBackend — server handles its own persistence.
        from nexus.contracts.capabilities import ConnectorCapability

        backend = mount_info["backend"]
        _caps: frozenset[str] = getattr(backend, "capabilities", frozenset())
        if (
            ConnectorCapability.EXTERNAL_CONTENT in _caps
            or getattr(backend, "name", "") == "remote"
        ):
            return

        # Map event type to operation type
        op_type = self._event_to_operation(event_type)
        if op_type is None:
            return

        self._backlog_store.enqueue(
            path=event.path,
            backend_name=mount_info["backend_name"],
            zone_id=event.zone_id or ROOT_ZONE_ID,
            operation_type=op_type,
            content_hash=event.etag,
            new_path=event.old_path if event_type == FileEventType.FILE_RENAME else None,
        )

    # ── Poll loop (pipe-driven with timer fallback) ──────────────────────

    async def _poll_loop(self) -> None:
        """Process pending backlog entries, woken by DT_PIPE or 30s timer.

        Two-tier wakeup (Issue #3194):
        1. DT_PIPE: immediate wakeup from enqueue() callback (us latency)
        2. Timer: 30s fallback if pipe unavailable or signal missed

        Follows the Kubernetes informer pattern: event-driven primary + periodic
        reconciliation as safety net.
        """
        while self._running:
            try:
                await self._process_all_backends()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WRITE_BACK] Poll loop error: {e}")

            # Wait for wakeup signal OR timeout (safety net)
            if self._pipe_manager is not None:
                try:
                    from nexus.bricks.ipc.wakeup import wait_for_signal

                    await wait_for_signal(
                        self._pipe_manager, _BACKLOG_WAKEUP_PIPE, timeout=self._poll_interval
                    )
                except asyncio.CancelledError:
                    break
                except Exception:
                    # Pipe error (closed, not found) — fall back to timer
                    await asyncio.sleep(self._poll_interval)
            else:
                await asyncio.sleep(self._poll_interval)

    async def _process_all_backends(self) -> None:
        """Process pending entries for all backends with pending work.

        Queries distinct (backend_name, zone_id) pairs from the backlog store
        rather than iterating mounts, ensuring non-default zones are processed.
        Backends are processed concurrently via asyncio.gather.
        """
        pairs = self._backlog_store.fetch_distinct_backend_zones()
        if not pairs:
            return
        await asyncio.gather(
            *(self._process_pending(name, zone) for name, zone in pairs),
            return_exceptions=True,
        )

    async def _process_pending(self, backend_name: str, zone_id: str) -> None:
        """Fetch and process a batch of pending entries for one backend."""
        entries = self._backlog_store.fetch_pending(backend_name, zone_id, limit=self._batch_size)
        if not entries:
            return

        tasks = [self._process_entry(entry) for entry in entries]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_entry(self, entry: "SyncBacklogEntry") -> None:
        """Process a single backlog entry with VFSSemaphore rate limiting.

        Issue #3194: Uses VFSSemaphore instead of asyncio.Semaphore for
        TTL auto-release on crash and holder tracking observability.
        """
        # Acquire a semaphore permit for this backend (async retry loop)
        sem_name = f"write_back:{entry.backend_name}"
        holder_id: str | None = None
        for _ in range(int(_SEMAPHORE_TTL_MS / (_SEMAPHORE_RETRY_INTERVAL * 1000))):
            holder_id = self._vfs_sem.acquire(
                sem_name,
                max_holders=self._max_concurrent,
                timeout_ms=0,
                ttl_ms=_SEMAPHORE_TTL_MS,
            )
            if holder_id is not None:
                break
            if not self._running:
                return  # Service shutting down, don't wait
            await asyncio.sleep(_SEMAPHORE_RETRY_INTERVAL)

        if holder_id is None:
            logger.warning("[WRITE_BACK] Timed out acquiring semaphore for %s", entry.backend_name)
            return

        try:
            if not self._backlog_store.mark_in_progress(entry.id):
                return  # Another worker claimed it
            try:
                await self._write_back_single(entry)
                self._backlog_store.mark_completed(entry.id)
                self._metrics.record_push(entry.backend_name)
                # Publish success event
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.SYNC_TO_BACKEND_COMPLETED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )
            except ConflictAbortError as e:
                logger.warning(f"[WRITE_BACK] Conflict ABORT on {entry.path}: {e}")
                self._backlog_store.mark_failed(entry.id, str(e))
                self._metrics.record_failure(entry.backend_name)
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.SYNC_TO_BACKEND_FAILED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )
            except Exception as e:
                logger.warning(f"[WRITE_BACK] Failed to write-back {entry.path}: {e}")
                self._backlog_store.mark_failed(entry.id, str(e))
                self._metrics.record_failure(entry.backend_name)
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.SYNC_TO_BACKEND_FAILED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )
        finally:
            self._vfs_sem.release(sem_name, holder_id)

    async def _write_back_single(self, entry: "SyncBacklogEntry") -> None:
        """Execute a single write-back operation to the backend.

        Steps:
        1. Resolve mount and backend for path
        2. Check for conflict (compare Nexus vs backend state)
        3. If conflict: resolve per strategy
        4. Execute backend operation
        5. Update change log with new state

        Args:
            entry: SyncBacklogEntry to process

        Raises:
            RuntimeError: If mount not found or backend operation fails
            ConflictAbortError: If ABORT strategy is active
        """
        mount_info = self._gw.get_mount_for_path(entry.path)
        if mount_info is None:
            raise RuntimeError(f"No mount found for path: {entry.path}")

        backend = mount_info["backend"]
        backend_path = mount_info["backend_path"]

        # Check for conflict before writing
        if entry.operation_type == "write":
            await self._handle_write(entry, backend, backend_path, mount_info)
        elif entry.operation_type == "delete":
            await self._handle_delete(backend, backend_path)
        elif entry.operation_type == "rename":
            await self._handle_rename(entry, backend, backend_path)
        elif entry.operation_type == "mkdir":
            await self._handle_mkdir(backend, backend_path)
        else:
            raise RuntimeError(f"Unsupported operation: {entry.operation_type}")

    async def _handle_write(
        self,
        entry: "SyncBacklogEntry",
        backend: Any,
        backend_path: str,
        mount_info: dict[str, Any],
    ) -> None:
        """Handle write-back of a file to the backend."""
        # Step 1: Check for conflict
        last_synced = self._change_log_store.get_change_log(
            entry.path, entry.backend_name, entry.zone_id
        )

        # Get current backend file info for conflict detection
        backend_file_info = None
        if hasattr(backend, "get_file_info"):
            try:
                backend_file_info = await asyncio.to_thread(backend.get_file_info, backend_path)
            except FileNotFoundError:
                pass  # File doesn't exist on backend yet — no conflict
            except Exception as exc:
                logger.debug("[WRITE_BACK] get_file_info failed for %s: %s", backend_path, exc)

        if backend_file_info is not None and last_synced is not None:
            # Get Nexus file info for comparison
            nexus_meta = self._gw.metadata_get(entry.path)
            nexus_mtime = getattr(nexus_meta, "mtime", None) if nexus_meta else None
            nexus_size = getattr(nexus_meta, "size", None) if nexus_meta else None
            nexus_hash = entry.content_hash

            is_conflict = detect_conflict(
                nexus_mtime=nexus_mtime,
                nexus_content_hash=nexus_hash,
                backend_file_info=backend_file_info,
                last_synced=last_synced,
            )

            if is_conflict:
                # Resolve per-mount strategy -> global default -> KEEP_NEWER
                strategy = self._resolve_strategy(mount_info)

                ctx = ConflictContext(
                    nexus_mtime=nexus_mtime,
                    nexus_size=nexus_size,
                    nexus_content_hash=nexus_hash,
                    backend_mtime=backend_file_info.mtime,
                    backend_size=getattr(backend_file_info, "size", None),
                    backend_content_hash=getattr(backend_file_info, "content_hash", None),
                    path=entry.path,
                    backend_name=entry.backend_name,
                    zone_id=entry.zone_id,
                )

                should_proceed = await self._resolve_and_act_on_conflict(entry, ctx, strategy)
                if not should_proceed:
                    return

        # Step 2: Read content from NexusFS and write to backend
        content = await self._read_nexus_content(entry.path)
        if content is None:
            raise RuntimeError(f"Failed to read content for {entry.path}")

        op_ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)
        result = await asyncio.to_thread(backend.write_content, content, op_ctx)
        # write_content now returns WriteResult directly, raises on error

        # Step 3: Update change log with new backend state
        new_hash = result.content_id
        self._change_log_store.upsert_change_log(
            path=entry.path,
            backend_name=entry.backend_name,
            zone_id=entry.zone_id,
            content_hash=new_hash,
        )

    async def _resolve_and_act_on_conflict(
        self,
        entry: "SyncBacklogEntry",
        ctx: ConflictContext,
        strategy: ConflictStrategy,
    ) -> bool:
        """Resolve conflict and return True if write should proceed.

        Args:
            entry: The backlog entry being processed
            ctx: ConflictContext with all metadata
            strategy: Which resolution strategy to apply

        Returns:
            True if the write-back should proceed, False to skip

        Raises:
            ConflictAbortError: If ABORT strategy is applied
        """
        outcome = resolve_conflict(ctx, strategy)

        # Record conflict metric (auto-resolved unless ABORT)
        auto_resolved = outcome != ResolutionOutcome.ABORT
        self._metrics.record_conflict(entry.backend_name, auto_resolved=auto_resolved)

        # Publish conflict event
        await self._event_bus.publish(
            FileEvent(
                type=FileEventType.CONFLICT_DETECTED,
                path=entry.path,
                zone_id=entry.zone_id,
            )
        )

        # Build and log conflict record
        conflict_copy_path: str | None = None
        if outcome == ResolutionOutcome.RENAME_CONFLICT:
            conflict_copy_path = self._generate_conflict_copy_path(entry.path, entry.backend_name)

        record = ConflictRecord(
            id=self._make_conflict_id(),
            path=ctx.path,
            backend_name=ctx.backend_name,
            zone_id=ctx.zone_id,
            strategy=strategy,
            outcome=outcome,
            nexus_content_hash=ctx.nexus_content_hash,
            nexus_mtime=ctx.nexus_mtime,
            nexus_size=ctx.nexus_size,
            backend_content_hash=ctx.backend_content_hash,
            backend_mtime=ctx.backend_mtime,
            backend_size=ctx.backend_size,
            conflict_copy_path=conflict_copy_path,
            status=ConflictStatus.AUTO_RESOLVED,
            resolved_at=datetime.now(UTC),
        )

        if self._conflict_log_store is not None:
            try:
                self._conflict_log_store.log_conflict(record)
            except Exception as e:
                logger.warning(f"[WRITE_BACK] Failed to log conflict: {e}")

        # Act on outcome
        match outcome:
            case ResolutionOutcome.ABORT:
                raise ConflictAbortError(
                    f"Conflict on {entry.path}: ABORT strategy — write-back halted"
                )
            case ResolutionOutcome.BACKEND_WINS:
                logger.info(
                    f"[WRITE_BACK] Conflict on {entry.path}: backend wins, skipping write-back"
                )
                return False
            case ResolutionOutcome.NEXUS_WINS:
                logger.info(
                    f"[WRITE_BACK] Conflict on {entry.path}: nexus wins, proceeding with write-back"
                )
                return True
            case ResolutionOutcome.RENAME_CONFLICT:
                logger.info(
                    f"[WRITE_BACK] Conflict on {entry.path}: creating conflict "
                    f"copy at {conflict_copy_path}"
                )
                assert conflict_copy_path is not None  # always set for RENAME_CONFLICT
                await self._create_conflict_copy(entry.path, conflict_copy_path)
                return True
            case _:
                logger.warning(
                    "[WRITE_BACK] Unhandled resolution outcome %s on %s, skipping",
                    outcome,
                    entry.path,
                )
                return False

    def _resolve_strategy(self, mount_info: dict[str, Any]) -> ConflictStrategy:
        """Resolve the conflict strategy for a mount.

        Resolution chain: mount.conflict_strategy -> global default -> KEEP_NEWER
        """
        mount_strategy = mount_info.get("conflict_strategy")
        if mount_strategy is not None:
            try:
                return ConflictStrategy(mount_strategy)
            except ValueError:
                pass
        return self._default_strategy

    async def _create_conflict_copy(self, original_path: str, conflict_path: str) -> None:
        """Create a NexusFS-side conflict copy of the file.

        Reads the current content and writes it to the conflict copy path.
        NexusFS-side only (CAS), near-free — following the Syncthing model.
        """
        try:
            content = await self._read_nexus_content(original_path)
            if content is not None:
                await self._gw.write(conflict_path, content)
        except Exception as e:
            logger.warning(f"[WRITE_BACK] Failed to create conflict copy: {e}")

    @staticmethod
    def _generate_conflict_copy_path(path: str, backend_name: str) -> str:
        """Generate a .sync-conflict copy path.

        Format: {dir}/{stem}.sync-conflict-{ISO timestamp}-{backend_name}{ext}
        """
        dir_part = posixpath.dirname(path)
        base = posixpath.basename(path)
        stem, ext = posixpath.splitext(base)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        conflict_name = f"{stem}.sync-conflict-{timestamp}-{backend_name}{ext}"
        return posixpath.join(dir_part, conflict_name)

    @staticmethod
    def _make_conflict_id() -> str:
        """Generate a UUID for a conflict record."""
        return str(uuid.uuid4())

    async def _handle_delete(self, backend: Any, backend_path: str) -> None:
        """Handle deletion of a file on the backend.

        Tries path-based delete() first (connector backends like local_connector),
        falling back to delete_content() for CAS backends.
        """
        ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)
        if hasattr(backend, "delete"):
            await asyncio.to_thread(backend.delete, backend_path, ctx)
        else:
            # delete_content raises on error, returns None on success
            await asyncio.to_thread(backend.delete_content, backend_path, ctx)

    async def _handle_rename(
        self,
        entry: "SyncBacklogEntry",
        backend: Any,
        backend_path: str,
    ) -> None:
        """Handle rename/move of a file on the backend.

        entry.path is the new (current) path; entry.new_path stores the old path.
        Tries backend.rename() or rename_file() first, falling back to
        delete-old + write-new.

        Args:
            entry: Backlog entry with path (new) and new_path (old)
            backend: Backend instance
            backend_path: Backend-relative new path
        """
        old_virtual_path = entry.new_path
        if old_virtual_path is None:
            raise RuntimeError(f"Rename entry missing old path for {entry.path}")

        # Resolve old path to backend-relative path
        old_mount_info = self._gw.get_mount_for_path(old_virtual_path)
        if old_mount_info is None:
            raise RuntimeError(f"No mount found for old path: {old_virtual_path}")
        old_backend_path = old_mount_info["backend_path"]

        ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)

        # Prefer native rename if available
        if hasattr(backend, "rename"):
            await asyncio.to_thread(backend.rename, old_backend_path, backend_path, ctx)
        elif hasattr(backend, "rename_file"):
            await asyncio.to_thread(backend.rename_file, old_backend_path, backend_path, ctx)
        else:
            # Fallback: delete old path, write new content
            await self._handle_delete(backend, old_backend_path)
            content = await self._read_nexus_content(entry.path)
            if content is None:
                raise RuntimeError(f"Failed to read content for rename of {entry.path}")
            op_ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)
            await asyncio.to_thread(backend.write_content, content, op_ctx)

    async def _handle_mkdir(self, backend: Any, backend_path: str) -> None:
        """Handle directory creation on the backend."""
        if not hasattr(backend, "mkdir"):
            raise RuntimeError(f"Backend {type(backend).__name__} does not support mkdir")
        ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)
        await asyncio.to_thread(backend.mkdir, backend_path, context=ctx)

    async def _read_nexus_content(self, path: str) -> bytes | None:
        """Read file content from NexusFS.

        Args:
            path: Virtual file path

        Returns:
            File content bytes, or None if not found
        """
        try:
            meta = self._gw.metadata_get(path)
            if meta is None:
                return None
            content_hash = getattr(meta, "content_hash", None)
            if content_hash is None:
                return None
            result = await self._gw.sys_read(path)
            if isinstance(result, bytes):
                return result
            return getattr(result, "data", None) if result else None
        except Exception as e:
            logger.warning(f"[WRITE_BACK] Failed to read {path}: {e}")
            return None

    def get_stats(self) -> dict[str, Any]:
        """Get write-back service statistics."""
        return {
            "running": self._running,
            "default_strategy": str(self._default_strategy),
            "max_concurrent_per_backend": self._max_concurrent,
            "poll_interval_seconds": self._poll_interval,
            "backlog_stats": self._backlog_store.get_stats(),
            "metrics": self._metrics.snapshot(),
        }

    def get_mount_for_path(self, path: str) -> dict[str, Any] | None:
        """Resolve mount info for a virtual path (public API for push endpoint)."""
        return self._gw.get_mount_for_path(path)

    def get_metrics_snapshot(self) -> dict[str, Any]:
        """Return a thread-safe copy of write-back metrics."""
        return self._metrics.snapshot()

    async def push_mount(self, backend_name: str, zone_id: str) -> None:
        """Trigger an immediate push for a specific backend/zone pair.

        Processes all pending backlog entries for the given backend and zone.
        """
        await self._process_pending(backend_name, zone_id)

    @staticmethod
    def _event_to_operation(event_type: FileEventType) -> str | None:
        """Map FileEventType to backlog operation type."""
        mapping = {
            FileEventType.FILE_WRITE: "write",
            FileEventType.FILE_DELETE: "delete",
            FileEventType.FILE_RENAME: "rename",
            FileEventType.DIR_CREATE: "mkdir",
            FileEventType.DIR_DELETE: "delete",
        }
        return mapping.get(event_type)
