"""WatchMixin — File watching syscall (inotify equivalent).

Tier 1: sys_watch (blocking wait for file changes)

Delegates to kernel FileWatcher primitive which races local OBSERVE
(in-memory futures, ~0µs) and optional remote watcher (federation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.file_watcher import FileWatcher


class WatchMixin:
    """File watching: sys_watch → FileWatcher."""

    # Provided by NexusFS.__init__
    _file_watcher: "FileWatcher"

    def _resolve_cred(self, context: Any) -> Any: ...

    @rpc_expose(description="Wait for file changes on a path")
    async def sys_watch(
        self,
        path: str,
        timeout: float = 30.0,
        *,
        recursive: bool = False,  # noqa: ARG002
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Wait for file changes (inotify(7)). Returns FileEvent dict or None on timeout.

        Delegates to kernel FileWatcher which races local OBSERVE + optional
        remote watcher (federation) via FIRST_COMPLETED.
        """
        path = validate_path(path, allow_root=True)
        ctx = self._resolve_cred(context)
        zone_id: str = getattr(ctx, "zone_id", None) or ROOT_ZONE_ID
        event = await self._file_watcher.wait(path, timeout=timeout, zone_id=zone_id)
        if event is None:
            return None
        result: dict[str, Any] = event.to_dict()
        return result
