"""Event operations for NexusFS.

This module provides file watching and locking operations for same-box scenarios:
- wait_for_changes: Watch for file system changes using OS-native APIs
- lock: Acquire advisory lock on a path
- unlock: Release advisory lock

These operations only work with PassthroughBackend in same-box mode.
For distributed scenarios, see Block 2: GlobalEventBus.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.file_watcher import FileWatcher
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class NexusFSEventsMixin:
    """Mixin providing event operations for NexusFS.

    Provides wait_for_changes, lock, and unlock operations for same-box
    file watching using OS-native APIs (inotify on Linux, ReadDirectoryChangesW
    on Windows).

    These methods are only available when using PassthroughBackend.
    """

    # Type hints for attributes from NexusFS parent class
    if TYPE_CHECKING:
        backend: Backend
        _file_watcher: FileWatcher | None

        def _validate_path(self, path: str) -> str: ...

    def _is_same_box(self) -> bool:
        """Check if we're in same-box mode (local file watching available).

        Returns:
            True if using PassthroughBackend, False otherwise
        """
        from nexus.backends.passthrough import PassthroughBackend

        return isinstance(self.backend, PassthroughBackend)

    def _get_file_watcher(self) -> "FileWatcher":
        """Get or create the file watcher instance.

        Returns:
            FileWatcher instance

        Raises:
            NotImplementedError: If not in same-box mode
        """
        if not self._is_same_box():
            raise NotImplementedError(
                "File watching is only available with PassthroughBackend (same-box mode). "
                "For distributed scenarios, use GlobalEventBus (Block 2)."
            )

        if not hasattr(self, "_file_watcher") or self._file_watcher is None:
            from nexus.core.file_watcher import FileWatcher

            self._file_watcher = FileWatcher()

        return self._file_watcher

    @rpc_expose(description="Wait for file system changes")
    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Wait for file system changes on a path.

        Watches the specified path for changes using OS-native file watching:
        - Linux: inotify
        - Windows: ReadDirectoryChangesW

        Semantics:
        - File path (e.g., "/inbox/file.txt"): Watches for content changes only
        - Directory path (e.g., "/inbox/"): Watches for file create/delete/rename

        Args:
            path: Virtual path to watch
            timeout: Maximum time to wait in seconds (default: 30.0)
            context: Operation context (optional)

        Returns:
            Dict with change info if change detected:
                - type: "created", "modified", "deleted", or "renamed"
                - path: Path that changed
                - old_path: Previous path (for rename events only)
            None if timeout reached

        Raises:
            NotImplementedError: If not using PassthroughBackend
            FileNotFoundError: If path does not exist
            PermissionError: If path is not accessible

        Example:
            >>> # Watch for new files in inbox
            >>> change = await nexus.wait_for_changes("/inbox/", timeout=60)
            >>> if change:
            ...     print(f"Detected {change['type']} on {change['path']}")
        """
        path = self._validate_path(path)

        # Get physical path from backend
        from nexus.backends.passthrough import PassthroughBackend

        if not isinstance(self.backend, PassthroughBackend):
            raise NotImplementedError(
                "wait_for_changes is only available with PassthroughBackend. "
                "For distributed scenarios, use GlobalEventBus (Block 2)."
            )

        physical_path = self.backend.get_physical_path(path)
        watcher = self._get_file_watcher()

        logger.debug(f"Watching for changes on {path} (physical: {physical_path})")

        change = await watcher.wait_for_change(physical_path, timeout=timeout)

        if change is None:
            return None

        return change.to_dict()

    @rpc_expose(description="Acquire advisory lock on a path")
    def lock(
        self,
        path: str,
        timeout: float = 30.0,
        context: "OperationContext | None" = None,
    ) -> str | None:
        """Acquire an advisory lock on a path.

        This is an in-memory lock for same-box coordination. Multiple processes
        on the same machine can use this to coordinate access to files.

        Args:
            path: Virtual path to lock
            timeout: Maximum time to wait for lock in seconds (default: 30.0)
            context: Operation context (optional)

        Returns:
            Lock ID if acquired (use this to unlock later)
            None if timeout reached

        Raises:
            NotImplementedError: If not using PassthroughBackend

        Example:
            >>> lock_id = nexus.lock("/shared/config.json", timeout=5.0)
            >>> if lock_id:
            ...     try:
            ...         # Perform exclusive operation
            ...         content = nexus.read("/shared/config.json")
            ...         nexus.write("/shared/config.json", modified_content)
            ...     finally:
            ...         nexus.unlock(lock_id)
            ... else:
            ...     print("Could not acquire lock")
        """
        path = self._validate_path(path)

        from nexus.backends.passthrough import PassthroughBackend

        if not isinstance(self.backend, PassthroughBackend):
            raise NotImplementedError(
                "lock is only available with PassthroughBackend. "
                "For distributed locking, use GlobalEventBus (Block 2)."
            )

        lock_id = self.backend.lock(path, timeout=timeout)

        if lock_id:
            logger.debug(f"Lock acquired on {path}: {lock_id}")
        else:
            logger.warning(f"Lock timeout on {path} after {timeout}s")

        return lock_id

    @rpc_expose(description="Release advisory lock")
    def unlock(
        self,
        lock_id: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Release an advisory lock.

        Args:
            lock_id: Lock ID returned from lock()
            context: Operation context (optional)

        Returns:
            True if lock was released
            False if lock_id was not found

        Raises:
            NotImplementedError: If not using PassthroughBackend

        Example:
            >>> lock_id = nexus.lock("/shared/config.json")
            >>> # ... do work ...
            >>> success = nexus.unlock(lock_id)
            >>> assert success
        """
        from nexus.backends.passthrough import PassthroughBackend

        if not isinstance(self.backend, PassthroughBackend):
            raise NotImplementedError(
                "unlock is only available with PassthroughBackend. "
                "For distributed locking, use GlobalEventBus (Block 2)."
            )

        released = self.backend.unlock(lock_id)

        if released:
            logger.debug(f"Lock released: {lock_id}")
        else:
            logger.warning(f"Lock not found: {lock_id}")

        return released