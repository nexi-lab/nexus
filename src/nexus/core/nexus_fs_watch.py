"""WatchMixin — File watching syscall (inotify equivalent).

Tier 1: sys_watch (blocking wait for file changes)

Delegates to Rust kernel sys_watch which blocks on Condvar until
a matching event arrives or timeout expires. Pure Rust FileWatcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.core.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


class WatchMixin:
    """File watching: sys_watch → Rust kernel FileWatcher."""

    _kernel: Any  # Rust Kernel

    @rpc_expose(description="Wait for file changes on a path")
    def sys_watch(
        self,
        path: str,
        timeout: float = 30.0,
        *,
        recursive: bool = False,  # noqa: ARG002
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Wait for file changes (inotify(7)). Blocks until event or timeout.

        Tier 1 syscall. Delegates to Rust kernel FileWatcher which blocks
        on Condvar. Returns dict with event_type + path, or None on timeout.
        """
        path = validate_path(path, allow_root=True)
        timeout_ms = max(1, int(timeout * 1000))
        result = self._kernel.sys_watch(path, timeout_ms)
        if result is None:
            return None
        event_type, event_path = result
        return {"type": event_type, "path": event_path}
