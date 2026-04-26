"""WatchMixin — File watching syscall (inotify equivalent).

Tier 1: sys_watch (blocking wait for file changes)

Delegates to Rust kernel sys_watch (FileWatchRegistry.wait_for_event).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.core.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


class WatchMixin:
    """File watching: sys_watch → Rust kernel."""

    _kernel: Any

    def _resolve_cred(self, context: Any) -> Any: ...

    @rpc_expose(description="Wait for file changes on a path")
    def sys_watch(
        self,
        path: str,
        timeout: float = 30.0,
        *,
        recursive: bool = False,  # noqa: ARG002
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Wait for file changes (inotify(7)). Returns FileEvent dict or None on timeout.

        Delegates to Rust kernel sys_watch (FileWatchRegistry.wait_for_event).
        """
        path = validate_path(path, allow_root=True)
        self._resolve_cred(context)  # validate credentials

        # Rust kernel path: blocking wait via Condvar (or stub returning None)
        if self._kernel is not None:
            event = self._kernel.sys_watch(path, int(timeout * 1000))
            if event is None:
                return None
            return {"event_type": event.event_type, "path": event.path}

        # No kernel: return None (watch not available)
        return None
