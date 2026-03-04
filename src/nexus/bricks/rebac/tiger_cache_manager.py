"""Tiger Cache management service extracted from NexusFS.

Handles Tiger Cache initialization, resource map syncing, and background
worker lifecycle. Operates independently of NexusFS via dependency injection.
"""

import logging
import os
import sys
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class TigerCacheManager:
    """Manages Tiger Cache performance optimizations.

    Extracted from NexusFS to decouple cache management from the filesystem.
    All dependencies are injected via the constructor.
    """

    def __init__(
        self,
        rebac_manager: Any,
        metadata_store: Any,
        default_zone_id: str,
        process_queue_fn: Callable | None = None,
        warm_cache_fn: Callable | None = None,
    ) -> None:
        self._rebac_manager = rebac_manager
        self._metadata_store = metadata_store
        self._default_zone_id = default_zone_id
        self._process_queue_fn = process_queue_fn
        self._warm_cache_fn = warm_cache_fn

        self._tiger_worker_stop: threading.Event | None = None
        self._tiger_worker_thread: threading.Thread | None = None

    def initialize(self) -> None:
        """Initialize performance optimizations for permission checks.

        This method:
        1. Syncs tiger_resource_map from existing metadata (Issue #934)
        2. Warms the Tiger Cache for faster subsequent permission checks
        3. Starts background worker for Tiger Cache queue processing

        Called automatically during startup. Can be called manually to refresh.
        """
        if os.getenv("NEXUS_DISABLE_PERF_OPTIMIZATIONS", "false").lower() in ("true", "1", "yes"):
            logger.debug("Performance optimizations disabled via environment variable")
            return

        try:
            # 1. Sync tiger_resource_map from existing metadata (Issue #934)
            # This MUST happen BEFORE cache warming so Tiger Cache can find resources
            if os.getenv("NEXUS_SYNC_TIGER_RESOURCE_MAP", "true").lower() in (
                "true",
                "1",
                "yes",
            ):
                synced = self.sync_resource_map()
                if synced > 0:
                    logger.info("Synced %d resources to Tiger resource map", synced)

            # 2. Warm Tiger Cache (optional, can be slow for large systems)
            # Only warm if explicitly enabled via environment variable
            if (
                os.getenv("NEXUS_WARM_TIGER_CACHE", "false").lower() in ("true", "1", "yes")
                and self._warm_cache_fn is not None
            ):
                entries = self._warm_cache_fn(zone_id=self._default_zone_id)
                if entries > 0:
                    logger.info("Warmed Tiger Cache with %d entries", entries)

            # 3. Start Tiger Cache background worker
            self.start_worker()

        except Exception as e:
            # Don't fail initialization if optimizations fail
            logger.warning("Failed to initialize performance optimizations: %s", e)

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

        try:
            count = 0
            log_interval = 1000

            for meta in self._metadata_store.list_iter("/", recursive=True):
                resource_map.get_or_create_int_id(
                    resource_type="file",
                    resource_id=meta.path,
                )
                count += 1

                if count % log_interval == 0:
                    logger.debug("Tiger resource map sync progress: %d resources...", count)

            logger.info("Tiger resource map sync complete: %d resources", count)
            return count

        except Exception as e:
            logger.warning("Failed to sync resource map from metadata: %s", e)
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

        # Don't start if already running
        if self._tiger_worker_thread is not None and self._tiger_worker_thread.is_alive():
            return

        # Worker interval in seconds (default: 1 second)
        interval = float(os.getenv("NEXUS_TIGER_WORKER_INTERVAL", "1.0"))

        # Shutdown flag
        self._tiger_worker_stop = threading.Event()

        # Capture callback reference for the closure
        process_queue_fn = self._process_queue_fn

        def worker_loop() -> None:
            """Background worker loop for Tiger Cache queue processing.

            NOTE: With write-through implemented, this worker is mainly for legacy
            queue entries. New permission grants are handled immediately by
            persist_single_grant() in rebac_write.
            """
            assert self._tiger_worker_stop is not None
            while not self._tiger_worker_stop.is_set():
                try:
                    if process_queue_fn is not None:
                        processed = process_queue_fn(batch_size=1)
                        if processed > 0:
                            logger.debug("Tiger Cache worker processed %d updates", processed)
                except Exception as e:
                    logger.warning("Tiger Cache worker error: %s", e)

                # Sleep longer since write-through handles new grants
                # This worker is just for legacy queue cleanup
                self._tiger_worker_stop.wait(timeout=interval * 10)

            logger.debug("Tiger Cache worker stopped")

        self._tiger_worker_thread = threading.Thread(
            target=worker_loop,
            name="tiger-cache-worker",
            daemon=True,
        )
        self._tiger_worker_thread.start()
        logger.debug("Tiger Cache worker started (interval=%ss)", interval)

    def stop_worker(self) -> None:
        """Stop the Tiger Cache background worker.

        Call this during graceful shutdown to stop the worker thread.
        """
        if self._tiger_worker_stop is not None:
            self._tiger_worker_stop.set()
        if self._tiger_worker_thread is not None:
            is_test = "pytest" in sys.modules
            timeout = 15.0 if is_test else 5.0
            self._tiger_worker_thread.join(timeout=timeout)
