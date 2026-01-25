"""Cross-platform file watcher for same-box event detection.

This module provides OS-native file watching using:
- Linux: inotify
- Windows: ReadDirectoryChangesW

Used by PassthroughBackend to implement wait_for_changes() for same-box scenarios.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ChangeType(Enum):
    """Type of file system change detected."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass
class FileChange:
    """Represents a detected file system change.

    Attributes:
        type: Type of change (created, modified, deleted, renamed)
        path: Path that changed (relative to watched path)
        old_path: Previous path for rename events, None otherwise
    """

    type: ChangeType
    path: str
    old_path: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, str | None] = {
            "type": self.type.value,
            "path": self.path,
        }
        if self.old_path:
            result["old_path"] = self.old_path
        return result


class FileWatcher:
    """Cross-platform file watcher using OS-native APIs.

    Automatically selects the appropriate implementation based on platform:
    - Linux: inotify via inotify_simple
    - Windows: ReadDirectoryChangesW via win32api

    Example:
        >>> watcher = FileWatcher()
        >>> change = await watcher.wait_for_change("/path/to/watch", timeout=30.0)
        >>> if change:
        ...     print(f"Detected {change.type.value} on {change.path}")
    """

    def __init__(self) -> None:
        """Initialize file watcher."""
        self._platform = sys.platform
        self._inotify = None  # Lazy-initialized on Linux

    async def wait_for_change(
        self,
        path: str | Path,
        timeout: float = 30.0,
    ) -> FileChange | None:
        """Wait for a file system change on the given path.

        Args:
            path: Path to watch (file or directory)
            timeout: Maximum time to wait in seconds (default: 30.0)

        Returns:
            FileChange if a change was detected, None if timeout

        Raises:
            NotImplementedError: If platform is not supported
            FileNotFoundError: If path does not exist
            PermissionError: If path is not accessible
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        if self._platform == "linux":
            return await self._wait_inotify(path, timeout)
        elif self._platform == "win32":
            return await self._wait_windows(path, timeout)
        else:
            raise NotImplementedError(
                f"Platform {self._platform} not supported. "
                "File watching requires Linux (inotify) or Windows (ReadDirectoryChangesW)."
            )

    async def _wait_inotify(self, path: Path, timeout: float) -> FileChange | None:
        """Wait for change using Linux inotify with recursive directory watching.

        Args:
            path: Path to watch
            timeout: Timeout in seconds

        Returns:
            FileChange or None on timeout
        """
        import os

        try:
            from inotify_simple import INotify, flags
        except ImportError as e:
            raise ImportError(
                "inotify_simple is required for Linux file watching. "
                "Install with: pip install inotify_simple"
            ) from e

        loop = asyncio.get_event_loop()
        inotify = INotify()
        wd_to_path: dict[int, Path] = {}

        try:
            watch_flags = (
                flags.CREATE | flags.DELETE | flags.MODIFY | flags.MOVED_TO | flags.MOVED_FROM
            )

            # Track if we're watching a specific file
            target_filename: str | None = None

            if path.is_dir():
                # Add watches recursively using os.walk (iterative, no stack overflow)
                for dirpath, _, _ in os.walk(path):
                    try:
                        wd = inotify.add_watch(dirpath, watch_flags)
                        wd_to_path[wd] = Path(dirpath)
                    except (PermissionError, FileNotFoundError):
                        pass
            else:
                # Watch parent directory for specific file
                target_filename = path.name
                wd = inotify.add_watch(str(path.parent), watch_flags)
                wd_to_path[wd] = path.parent

            def read_events() -> list:
                return inotify.read(timeout=int(timeout * 1000))

            try:
                events = await asyncio.wait_for(
                    loop.run_in_executor(None, read_events),
                    timeout=timeout + 1.0,
                )
            except TimeoutError:
                return None

            if not events:
                return None

            # Find matching event (filter by filename if watching specific file)
            for event in events:
                # If watching a specific file, only match events for that file
                if target_filename is not None and event.name != target_filename:
                    continue

                event_flags = event.mask

                # Map inotify flags to ChangeType
                if event_flags & flags.CREATE:
                    change_type = ChangeType.CREATED
                elif event_flags & flags.DELETE:
                    change_type = ChangeType.DELETED
                elif event_flags & flags.MODIFY:
                    change_type = ChangeType.MODIFIED
                elif event_flags & (flags.MOVED_TO | flags.MOVED_FROM):
                    change_type = ChangeType.RENAMED
                else:
                    change_type = ChangeType.MODIFIED

                # Build relative path from watched root
                watch_dir = wd_to_path.get(event.wd, path)
                if event.name:
                    full_path = watch_dir / event.name
                    try:
                        changed_path = str(full_path.relative_to(path))
                    except ValueError:
                        changed_path = event.name
                else:
                    changed_path = str(watch_dir)

                return FileChange(type=change_type, path=changed_path)

            # No matching event found
            return None

        finally:
            inotify.close()

    async def _wait_windows(self, path: Path, timeout: float) -> FileChange | None:
        """Wait for change using Windows ReadDirectoryChangesW with overlapped I/O.

        Uses overlapped (async) I/O to support proper timeout handling.

        Args:
            path: Path to watch
            timeout: Timeout in seconds

        Returns:
            FileChange or None on timeout
        """
        try:
            import pywintypes
            import win32api
            import win32event
            import win32file
        except ImportError as e:
            raise ImportError(
                "pywin32 is required for Windows file watching. Install with: pip install pywin32"
            ) from e

        loop = asyncio.get_event_loop()

        # Windows constants
        FILE_LIST_DIRECTORY = 1
        FILE_SHARE_READ = 1
        FILE_SHARE_WRITE = 2
        FILE_SHARE_DELETE = 4
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_FLAG_OVERLAPPED = 0x40000000
        FILE_NOTIFY_CHANGE_FILE_NAME = 1
        FILE_NOTIFY_CHANGE_DIR_NAME = 2
        FILE_NOTIFY_CHANGE_SIZE = 8
        FILE_NOTIFY_CHANGE_LAST_WRITE = 16
        WAIT_TIMEOUT = 258
        WAIT_OBJECT_0 = 0

        # For files, watch the parent directory
        watch_path = path if path.is_dir() else path.parent
        target_file = None if path.is_dir() else path.name

        # Open directory handle with overlapped flag for async I/O
        handle = win32file.CreateFile(
            str(watch_path),
            FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
            None,
        )

        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)

        try:
            # Watch flags
            watch_flags = (
                FILE_NOTIFY_CHANGE_FILE_NAME
                | FILE_NOTIFY_CHANGE_DIR_NAME
                | FILE_NOTIFY_CHANGE_SIZE
                | FILE_NOTIFY_CHANGE_LAST_WRITE
            )

            def read_changes_with_timeout() -> list | None:
                """Read directory changes with timeout support via overlapped I/O."""
                buf = win32file.AllocateReadBuffer(8192)  # Larger buffer for recursive watching

                # Start async read
                win32file.ReadDirectoryChangesW(
                    handle,
                    buf,
                    True,  # Watch subtree (True = recursive, watches all subdirectories)
                    watch_flags,
                    overlapped,
                )

                # Wait with timeout (convert seconds to milliseconds)
                timeout_ms = int(timeout * 1000)
                rc = win32event.WaitForSingleObject(overlapped.hEvent, timeout_ms)

                if rc == WAIT_TIMEOUT:
                    # Cancel the pending I/O operation
                    with contextlib.suppress(Exception):
                        win32file.CancelIo(handle)
                    return None
                elif rc == WAIT_OBJECT_0:
                    # Get the result
                    try:
                        nbytes = win32file.GetOverlappedResult(handle, overlapped, False)
                        if nbytes:
                            return win32file.FILE_NOTIFY_INFORMATION(buf, nbytes)
                    except Exception:
                        pass
                    return []
                else:
                    return None

            results = await loop.run_in_executor(None, read_changes_with_timeout)

            if not results:
                return None

            # Map Windows action codes to ChangeType
            # Action codes: 1=Added, 2=Removed, 3=Modified, 4=RenamedOld, 5=RenamedNew
            action_map = {
                1: ChangeType.CREATED,
                2: ChangeType.DELETED,
                3: ChangeType.MODIFIED,
                4: ChangeType.RENAMED,
                5: ChangeType.RENAMED,
            }

            for action, filename in results:
                # If watching specific file, filter for it
                if target_file and filename != target_file:
                    continue

                change_type = action_map.get(action, ChangeType.MODIFIED)
                return FileChange(type=change_type, path=filename)

            return None

        finally:
            win32api.CloseHandle(overlapped.hEvent)
            win32api.CloseHandle(handle)

    def close(self) -> None:
        """Clean up any resources."""
        if self._inotify is not None:
            self._inotify.close()
            self._inotify = None
