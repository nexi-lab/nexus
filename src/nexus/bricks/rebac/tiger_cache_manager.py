"""Tiger Cache management service extracted from NexusFS.

Handles Tiger Cache initialization, resource map syncing, and background
worker lifecycle. Operates independently of NexusFS via dependency injection.
"""

import logging
import os
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS


logger = logging.getLogger(__name__)

# Issue #4342: bound on how long initialize() may hold the boot thread while
# the resource-map sync runs. Mirrors the search daemon's startup preload
# (_PRELOAD_TIMEOUT_SECONDS in bricks/search/daemon.py): boot must not block
# indefinitely on a perf optimization.
_DEFAULT_INIT_TIMEOUT_SECONDS = 30.0


class TigerCacheManager:
    """Manages Tiger Cache performance optimizations.

    Extracted from NexusFS to decouple cache management from the filesystem.
    All dependencies are injected via the constructor.
    """

    def __init__(
        self,
        rebac_manager: Any,
        nexus_fs: "NexusFS",
        default_zone_id: str,
        process_queue_fn: Callable | None = None,
        warm_cache_fn: Callable | None = None,
    ) -> None:
        self._rebac_manager = rebac_manager
        # Resource-map sync lists the filesystem through the §2.2 syscall
        # surface (sys_readdir), not the kernel-internal MetaStore pillar.
        self._nexus_fs = nexus_fs
        self._default_zone_id = default_zone_id
        self._process_queue_fn = process_queue_fn
        self._warm_cache_fn = warm_cache_fn

        self._tiger_worker_stop: threading.Event | None = None
        self._tiger_worker_thread: threading.Thread | None = None
        self._sync_thread: threading.Thread | None = None
        self._sync_stop = threading.Event()
        # Serializes initialize()/start_worker()/stop_worker() so a manual
        # refresh cannot clear a stop event an in-flight shutdown just set,
        # and sync stays single-flight (Codex review, Issue #4342). RLock:
        # initialize() calls start_worker() while holding it.
        self._lifecycle_lock = threading.RLock()
        # Terminal once stop_worker() ran: a boot thread resuming from its
        # bounded join (held outside the lock) must not start the worker,
        # and a manual initialize() must not spawn a new sync.
        self._stopped = False

    def initialize(self) -> None:
        """Initialize performance optimizations for permission checks.

        This method:
        1. Syncs tiger_resource_map from existing metadata (Issue #934)
        2. Warms the Tiger Cache for faster subsequent permission checks
        3. Starts background worker for Tiger Cache queue processing

        Called automatically during startup. Can be called manually to refresh.

        Issue #4342: steps 1-2 are DB-bound and used to run unbounded on the
        boot thread — a wedged metastore/DB call blocks instead of raising,
        so the except below never fires and the port is never bound. They now
        run on a daemon thread; boot waits at most
        NEXUS_TIGER_INIT_TIMEOUT_SECONDS (default 30, 0 = don't wait) and
        then proceeds, leaving the sync to finish in the background.
        Permission checks fall back to the slow path until it completes.
        """
        if os.getenv("NEXUS_DISABLE_PERF_OPTIMIZATIONS", "false").lower() in ("true", "1", "yes"):
            logger.debug("Performance optimizations disabled via environment variable")
            return

        try:
            import math

            timeout_raw = os.getenv("NEXUS_TIGER_INIT_TIMEOUT_SECONDS", "")
            try:
                timeout = float(timeout_raw) if timeout_raw else _DEFAULT_INIT_TIMEOUT_SECONDS
            except ValueError:
                timeout = math.nan  # rejected below
            # Contract: finite seconds >= 0 (0 = don't wait). inf raises
            # OverflowError inside Thread.join (aborting initialize via the
            # outer except); nan/negative silently skip the join.
            if not math.isfinite(timeout) or timeout < 0:
                logger.warning(
                    "Invalid NEXUS_TIGER_INIT_TIMEOUT_SECONDS=%r; using %.0fs",
                    timeout_raw,
                    _DEFAULT_INIT_TIMEOUT_SECONDS,
                )
                timeout = _DEFAULT_INIT_TIMEOUT_SECONDS
            timeout = min(timeout, threading.TIMEOUT_MAX)

            with self._lifecycle_lock:
                if self._stopped:
                    logger.info("TigerCacheManager stopped; skipping initialize")
                    return
                # Single-flight: a manual refresh while the previous sync is
                # still running would orphan that thread and (via a cleared
                # stop event) could undo a concurrent shutdown signal.
                if self._sync_thread is not None and self._sync_thread.is_alive():
                    logger.info("Tiger resource map sync already running; skipping re-initialize")
                    self.start_worker()
                    return
                # Fresh event per run (never clear()) so an older thread that
                # was told to stop keeps seeing its own event as set.
                stop_event = threading.Event()
                self._sync_stop = stop_event
                sync_thread = threading.Thread(
                    target=self._sync_and_warm,
                    args=(stop_event,),
                    name="tiger-init-sync",
                    daemon=True,
                )
                self._sync_thread = sync_thread
                sync_thread.start()
            if timeout > 0:
                sync_thread.join(timeout=timeout)
            if sync_thread.is_alive():
                logger.warning(
                    "Tiger resource map sync still running after %.0fs; continuing "
                    "boot without it — permission checks use the slow path until "
                    "the background sync completes (Issue #4342)",
                    timeout,
                )

            # Start Tiger Cache background worker (thread spawn only — safe
            # on the boot thread, independent of the sync above)
            self.start_worker()

        except Exception as e:
            # Don't fail initialization if optimizations fail
            logger.warning(f"Failed to initialize performance optimizations: {e}")

    def _sync_and_warm(self, stop_event: threading.Event) -> None:
        """Resource-map sync + optional cache warm (runs on tiger-init-sync).

        Everything here is fail-soft: failures only mean permission checks
        keep paying the slow-path cost they would pay anyway. ``stop_event``
        is this run's own event (captured, not read from self) so a later
        re-initialize cannot detach this thread from its shutdown signal.
        """
        try:
            # 1. Sync tiger_resource_map from existing metadata (Issue #934)
            # This MUST happen BEFORE cache warming so Tiger Cache can find resources
            if os.getenv("NEXUS_SYNC_TIGER_RESOURCE_MAP", "true").lower() in (
                "true",
                "1",
                "yes",
            ):
                synced = self.sync_resource_map()
                if synced > 0 and not stop_event.is_set():
                    logger.info(f"Synced {synced} resources to Tiger resource map")

            # A stop request that ended the sync early must not fall through
            # into another DB-bound phase (Codex round 2).
            if stop_event.is_set():
                return

            # 2. Warm Tiger Cache (optional, can be slow for large systems)
            # Only warm if explicitly enabled via environment variable
            if (
                os.getenv("NEXUS_WARM_TIGER_CACHE", "false").lower() in ("true", "1", "yes")
                and self._warm_cache_fn is not None
            ):
                entries = self._warm_cache_fn(zone_id=self._default_zone_id)
                if entries > 0:
                    logger.info(f"Warmed Tiger Cache with {entries} entries")
        except Exception as e:
            logger.warning(f"Failed to initialize performance optimizations: {e}")

    def sync_resource_map(self) -> int:
        """Populate tiger_resource_map from existing metadata.

        Issue #934: Enables Tiger Cache to work for pre-existing files by
        ensuring all files have integer IDs in the resource map.

        This fixes the chicken-and-egg problem where:
        - Tiger Cache needs resource IDs to check access
        - Resource IDs were only created during permission checks
        - Permission checks returned cache miss -> never populated

        Returns:
            Number of resources synced to the map

        Performance:
            ~5 seconds for 6,000 files (one-time startup cost)

        Environment:
            NEXUS_SYNC_TIGER_RESOURCE_MAP: Set to "false" to disable (default: true)
        """
        if self._rebac_manager is None:
            logger.debug("No ReBAC manager - skipping resource map sync")
            return 0

        tiger_cache = getattr(self._rebac_manager, "_tiger_cache", None)
        if not tiger_cache:
            logger.debug("Tiger Cache disabled - skipping resource map sync")
            return 0

        resource_map = getattr(tiger_cache, "_resource_map", None)
        if not resource_map:
            logger.debug("No resource map in Tiger Cache - skipping sync")
            return 0

        if self._nexus_fs is None:
            logger.debug("No NexusFS handle - skipping resource map sync")
            return 0

        try:
            count = 0
            log_interval = 1000

            from nexus.contracts.types import OperationContext

            sys_ctx = OperationContext(user_id="system", groups=[], is_system=True)
            for entry_path in self._nexus_fs.sys_readdir(
                "/", recursive=True, details=False, context=sys_ctx
            ):
                if self._sync_stop.is_set():
                    logger.info(
                        "Tiger resource map sync stopped after %d resources (shutdown)",
                        count,
                    )
                    return count
                if not entry_path:
                    continue
                resource_map.get_or_create_int_id(
                    resource_type="file",
                    resource_id=str(entry_path),
                )
                count += 1

                if count % log_interval == 0:
                    logger.debug(f"Tiger resource map sync progress: {count} resources...")

            logger.info(f"Tiger resource map sync complete: {count} resources")
            return count

        except Exception as e:
            logger.warning(f"Failed to sync resource map from metadata: {e}")
            return 0

    def start_worker(self) -> None:
        """Start background thread for Tiger Cache queue processing.

        NOTE: With write-through implemented, automatic queue processing is
        DISABLED by default. Write-through handles grants/revokes immediately.

        Queue processing is only needed for:
        - Cold start cache warming (use warm_tiger_cache() explicitly)
        - Bulk migrations
        - Group permission inheritance changes

        To enable automatic queue processing, set:
            NEXUS_ENABLE_TIGER_WORKER=true
        """
        if os.getenv("NEXUS_ENABLE_TIGER_WORKER", "false").lower() not in ("true", "1", "yes"):
            logger.debug("Tiger Cache queue worker disabled (write-through handles grants)")
            return

        with self._lifecycle_lock:
            # Terminal stop: a boot thread that resumed from its bounded
            # join after stop_worker() completed lands here — don't revive
            # background work after shutdown began.
            if self._stopped:
                logger.debug("TigerCacheManager stopped; not starting worker")
                return
            # Don't start if already running
            if self._tiger_worker_thread is not None and self._tiger_worker_thread.is_alive():
                return

            # Worker interval in seconds (default: 1 second)
            interval = float(os.getenv("NEXUS_TIGER_WORKER_INTERVAL", "1.0"))

            # Per-run shutdown flag, captured by the closure (never read from
            # self) so a later start_worker() cannot detach a still-running
            # loop from the stop signal it was given (Codex round 2).
            worker_stop = threading.Event()
            self._tiger_worker_stop = worker_stop

            # Capture callback reference for the closure
            process_queue_fn = self._process_queue_fn

            def worker_loop() -> None:
                """Background worker loop for Tiger Cache queue processing.

                NOTE: With write-through implemented, this worker is mainly for legacy
                queue entries. New permission grants are handled immediately by
                persist_single_grant() in rebac_write.
                """
                while not worker_stop.is_set():
                    try:
                        if process_queue_fn is not None:
                            processed = process_queue_fn(batch_size=1)
                            if processed > 0:
                                logger.debug(f"Tiger Cache worker processed {processed} updates")
                    except Exception as e:
                        logger.warning(f"Tiger Cache worker error: {e}")

                    # Sleep longer since write-through handles new grants
                    # This worker is just for legacy queue cleanup
                    worker_stop.wait(timeout=interval * 10)

                logger.debug("Tiger Cache worker stopped")

            self._tiger_worker_thread = threading.Thread(
                target=worker_loop,
                name="tiger-cache-worker",
                daemon=True,
            )
            self._tiger_worker_thread.start()
            logger.debug(f"Tiger Cache worker started (interval={interval}s)")

    def stop_worker(self) -> None:
        """Stop the Tiger Cache background worker and any in-progress sync.

        Call this during graceful shutdown to stop the worker thread.
        """
        is_test = "pytest" in sys.modules
        timeout = 15.0 if is_test else 5.0
        with self._lifecycle_lock:
            self._stopped = True
            # Signal BOTH threads before joining either, so the worker does
            # not keep running for the duration of the sync join.
            self._sync_stop.set()
            if self._tiger_worker_stop is not None:
                self._tiger_worker_stop.set()
            if self._sync_thread is not None:
                self._sync_thread.join(timeout=timeout)
                if self._sync_thread.is_alive():
                    # The sync observes the stop event between per-path
                    # upserts, but a call wedged inside sys_readdir or a
                    # single DB statement cannot be interrupted from Python.
                    # The thread is a daemon, so it cannot block process
                    # exit; it may log fail-soft warnings if it touches
                    # closed resources afterwards.
                    logger.warning(
                        "tiger-init-sync still running %.0fs after shutdown "
                        "request; abandoning daemon thread (Issue #4342)",
                        timeout,
                    )
            if self._tiger_worker_thread is not None:
                self._tiger_worker_thread.join(timeout=timeout)
