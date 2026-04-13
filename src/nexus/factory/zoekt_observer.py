"""OBSERVE-phase observer for Zoekt search index updates.

Receives FILE_WRITE events from the Rust kernel and triggers Zoekt
reindex after a debounce window.

Issue #810: Decouple Zoekt on_write_callback sync from ObjectStore write path.

Architecture:
    Rust kernel sys_write -> dispatch_observers (MutationObserver trait)
      -> accumulate path in set + reset debounce timer
      -> threading.Timer fires _flush()
      -> ZoektIndexManager.trigger_reindex_sync()
"""

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.search.zoekt_client import ZoektIndexManager

logger = logging.getLogger(__name__)


class ZoektWriteObserver:
    """OBSERVE-phase observer for Zoekt search index updates.

    Receives FILE_WRITE events from the Rust kernel and triggers Zoekt
    reindex after a debounce window.

    Registration:
        Enlisted via factory orchestrator; events dispatched by the Rust
        kernel's MutationObserver trait (not Python on_mutation).
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

        Fallback path for CLI mode where the observer is not registered
        in KernelDispatch.
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
