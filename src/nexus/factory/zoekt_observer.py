"""OBSERVE-phase observer for Zoekt search index updates.

Receives FILE_WRITE events via Rust dispatch_observers and triggers
Zoekt reindex after a debounce window. Replaces the former DT_PIPE-based ZoektPipeConsumer -- zero blocking I/O, zero async lifecycle.

Issue #810: Decouple Zoekt on_write_callback sync from ObjectStore write path.

Architecture (before -- DT_PIPE, now deleted):
    CASLocalBackend.write_content() (sync)
      -> ZoektPipeConsumer.notify_write(path)   [deleted]
        -> deque buffer -> flush task -> sys_write  # blocking I/O
        -> async consumer (_consume loop)
        -> trigger_reindex_async()

Architecture (after -- OBSERVE):
    Rust kernel sys_write dispatch_observers
      -> ZoektWriteObserver.on_mutation(FileEvent)
        -> accumulate path in set + reset debounce timer
        -> threading.Timer fires _flush()
        -> ZoektIndexManager.trigger_reindex_async()
"""

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.search.zoekt_client import ZoektIndexManager

logger = logging.getLogger(__name__)


class ZoektWriteObserver:
    """OBSERVE-phase observer for Zoekt search index updates.

    Receives FILE_WRITE events via Rust dispatch_observers and triggers
    Zoekt reindex after a debounce window. Replaces DT_PIPE-based
    the former DT_PIPE-based ZoektPipeConsumer -- zero blocking I/O, zero async lifecycle.

    Registration:
        Enlisted via factory orchestrator (hook_spec duck-typed),
        NOT via bind_fs + start/stop async lifecycle.
    """

    def __init__(
        self,
        zoekt_index_manager: "ZoektIndexManager",
        *,
        debounce_seconds: float | None = None,
    ) -> None:
        self._zoekt = zoekt_index_manager
        self._debounce = debounce_seconds or zoekt_index_manager.debounce_seconds

        # Debounce state — protected by _lock
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Debounce flush
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Fire after debounce window -- trigger Zoekt reindex."""
        with self._lock:
            paths = self._pending.copy()
            self._pending.clear()
            self._timer = None

        if not paths:
            return

        count = len(paths)
        try:
            self._zoekt.trigger_reindex_sync()
            logger.debug("Zoekt reindex triggered (%d pending paths)", count)
        except Exception as e:
            logger.error("Zoekt reindex failed: %s", e)

    # ------------------------------------------------------------------
    # Legacy sync callbacks (CLI mode / CASLocalBackend fallback)
    # ------------------------------------------------------------------

    def notify_write(self, path: str) -> None:
        """Sync callback for CASLocalBackend.on_write_callback.

        When registered as a VFS observer, events arrive via on_mutation().
        This method exists for fallback / CLI mode where the observer is
        not registered in KernelDispatch.
        """
        self._zoekt.notify_write(path)

    def notify_sync_complete(self, files_synced: int = 0) -> None:
        """Sync callback for CASLocalBackend.on_sync_callback.

        Fallback path -- forwards directly to ZoektIndexManager.
        """
        self._zoekt.notify_sync_complete(files_synced)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Cancel any pending debounce timer (for clean shutdown)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
