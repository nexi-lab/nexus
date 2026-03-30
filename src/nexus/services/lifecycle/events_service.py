"""Events Service — file watching RPC wrapper.

Thin service-layer wrapper around kernel FileWatcher (§4.3).
Exposes ``wait_for_changes()`` via RPC.

Advisory locking moved to kernel syscalls (sys_lock/sys_unlock) in Phase 5.
Lock methods deleted — use NexusFS.sys_lock()/sys_unlock()/locked() instead.

Phase 2: Core Refactoring (Issue #1287)
Extracted from: nexus_fs_events.py (836 lines)
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.file_watcher import FileWatcher


class EventsService:
    """Events service — RPC wrapper for kernel FileWatcher.

    File watching is fully delegated to the kernel FileWatcher primitive.
    This service adds:
    - ``@rpc_expose`` for gRPC/HTTP access
    - Zone ID resolution from OperationContext

    Advisory locking is now on NexusFS kernel (sys_lock/sys_unlock/locked).
    """

    def __init__(
        self,
        file_watcher: "FileWatcher",
        zone_id: str | None = None,
    ):
        self._file_watcher = file_watcher
        self._zone_id = zone_id

        logger.info("[EventsService] Initialized (delegates to kernel FileWatcher)")

    def _get_zone_id(self, context: "OperationContext | None") -> str:
        """Get zone ID from context or default."""
        if context and hasattr(context, "zone_id") and context.zone_id:
            return context.zone_id
        if self._zone_id:
            return self._zone_id
        return ROOT_ZONE_ID

    # =========================================================================
    # Cache Invalidation Hooks (used by multi-instance tests)
    # =========================================================================

    def _start_cache_invalidation(self) -> None:
        """No-op placeholder for multi-instance test fixtures."""
        logger.debug("[EventsService] _start_cache_invalidation (no-op)")

    def _stop_cache_invalidation(self) -> None:
        """No-op placeholder — see ``_start_cache_invalidation``."""
        logger.debug("[EventsService] _stop_cache_invalidation (no-op)")

    # =========================================================================
    # Public API: File Watching
    # =========================================================================

    @rpc_expose(description="Wait for file system changes")
    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
        _context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Wait for file system changes on a path.

        Delegates to kernel FileWatcher which races local OBSERVE (~0µs)
        and optional remote watcher (distributed) via FIRST_COMPLETED.

        Args:
            path: Virtual path to watch (supports glob patterns)
            timeout: Maximum time to wait in seconds (default: 30.0)
            _context: Operation context (optional)

        Returns:
            Dict with change info if change detected, None if timeout
        """
        path = validate_path(path, allow_root=True)
        zone_id = self._get_zone_id(_context)

        event = await self._file_watcher.wait(path, timeout=timeout, zone_id=zone_id)
        if event is None:
            return None
        return event.to_dict()
