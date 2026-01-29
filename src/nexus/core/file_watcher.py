"""Cross-platform file watcher for same-box event detection.

This module provides OS-native file watching using:
- Linux: inotify + asyncio add_reader (true event-driven)
- Windows: ReadDirectoryChangesW + RegisterWaitForSingleObject (true callback)

Architecture:
- FileWatcher: Persistent watcher with callback registration
- add_watch(path, callback): Register callback for path changes
- Events are delivered via callbacks, no polling required

Used by NexusFS for:
1. wait_for_changes() API - user-facing file change notifications
2. Cache invalidation - automatic cache updates on external changes
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


# Type alias for callback functions
FileChangeCallback = Callable[[FileChange], None]


class FileWatcher:
    """Cross-platform file watcher using OS-native event-driven APIs.

    Uses true event-driven callbacks (no polling):
    - Linux: inotify fd registered with asyncio event loop via add_reader()
    - Windows: RegisterWaitForSingleObject for OS-level callbacks

    Example (callback mode):
        >>> watcher = FileWatcher()
        >>> watcher.start()
        >>> watcher.add_watch("/inbox", lambda change: print(f"Changed: {change.path}"))
        >>> # ... events delivered via callbacks ...
        >>> watcher.stop()

    Example (one-shot mode, backward compatible):
        >>> watcher = FileWatcher()
        >>> change = await watcher.wait_for_change("/inbox", timeout=30.0)
    """

    def __init__(self) -> None:
        """Initialize file watcher."""
        self._platform = sys.platform
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None

        # Platform-specific state
        if self._platform == "linux":
            self._inotify: Any = None
            self._wd_to_info: dict[int, tuple[Path, FileChangeCallback]] = {}
        elif self._platform == "win32":
            self._watches: dict[str, _WindowsWatch] = {}

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the file watcher.

        Must be called before add_watch(). Initializes OS resources and
        registers with the event loop.

        Args:
            loop: Event loop to use (defaults to current running loop)
        """
        if self._started:
            return

        self._loop = loop or asyncio.get_event_loop()

        if self._platform == "linux":
            self._start_linux()
        elif self._platform == "win32":
            self._start_windows()
        else:
            raise NotImplementedError(
                f"Platform {self._platform} not supported. "
                "File watching requires Linux (inotify) or Windows (ReadDirectoryChangesW)."
            )

        self._started = True
        logger.info(f"FileWatcher started (platform: {self._platform})")

    def stop(self) -> None:
        """Stop the file watcher and release all resources."""
        if not self._started:
            return

        if self._platform == "linux":
            self._stop_linux()
        elif self._platform == "win32":
            self._stop_windows()

        self._started = False
        self._loop = None
        logger.info("FileWatcher stopped")

    def add_watch(
        self,
        path: str | Path,
        callback: FileChangeCallback,
        recursive: bool = True,
    ) -> None:
        """Add a watch for file system changes on the given path.

        Args:
            path: Path to watch (file or directory)
            callback: Function called when changes are detected
            recursive: Watch subdirectories recursively (default: True)

        Raises:
            RuntimeError: If watcher not started
            FileNotFoundError: If path does not exist
        """
        if not self._started:
            raise RuntimeError("FileWatcher not started. Call start() first.")

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        if self._platform == "linux":
            self._add_watch_linux(path, callback, recursive)
        elif self._platform == "win32":
            self._add_watch_windows(path, callback, recursive)

        logger.debug(f"Added watch: {path} (recursive={recursive})")

    def remove_watch(self, path: str | Path) -> None:
        """Remove a watch for the given path.

        Args:
            path: Path to stop watching
        """
        if not self._started:
            return

        path = Path(path)

        if self._platform == "linux":
            self._remove_watch_linux(path)
        elif self._platform == "win32":
            self._remove_watch_windows(path)

        logger.debug(f"Removed watch: {path}")

    # =========================================================================
    # Linux Implementation (inotify + asyncio add_reader)
    # =========================================================================

    def _start_linux(self) -> None:
        """Initialize inotify and register with event loop."""
        try:
            from inotify_simple import INotify
        except ImportError as e:
            raise ImportError(
                "inotify_simple is required for Linux file watching. "
                "Install with: pip install inotify_simple"
            ) from e

        self._inotify = INotify()
        self._wd_to_info = {}

        # Register inotify fd with asyncio event loop
        # When fd is readable, _on_inotify_events will be called
        self._loop.add_reader(self._inotify.fd, self._on_inotify_events)  # type: ignore[union-attr]

    def _stop_linux(self) -> None:
        """Clean up inotify resources."""
        if self._inotify is None:
            return

        # Unregister from event loop
        if self._loop:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._inotify.fd)

        # Remove all watches
        for wd in list(self._wd_to_info.keys()):
            with contextlib.suppress(Exception):
                self._inotify.rm_watch(wd)

        self._wd_to_info.clear()
        self._inotify.close()
        self._inotify = None

    def _add_watch_linux(self, path: Path, callback: FileChangeCallback, recursive: bool) -> None:
        """Add inotify watch for path."""
        from inotify_simple import flags

        watch_flags = (
            flags.CREATE
            | flags.DELETE
            | flags.MODIFY
            | flags.MOVED_TO
            | flags.MOVED_FROM
            | flags.DELETE_SELF
            | flags.MOVE_SELF
        )

        if path.is_dir():
            if recursive:
                # Add watches recursively
                for dirpath, _, _ in os.walk(path):
                    try:
                        wd = self._inotify.add_watch(dirpath, watch_flags)
                        self._wd_to_info[wd] = (Path(dirpath), callback)
                    except (PermissionError, FileNotFoundError, OSError):
                        pass
            else:
                wd = self._inotify.add_watch(str(path), watch_flags)
                self._wd_to_info[wd] = (path, callback)
        else:
            # Watch parent directory for specific file
            wd = self._inotify.add_watch(str(path.parent), watch_flags)
            self._wd_to_info[wd] = (path.parent, callback)

    def _remove_watch_linux(self, path: Path) -> None:
        """Remove inotify watch for path."""
        wds_to_remove = [
            wd
            for wd, (watched_path, _) in self._wd_to_info.items()
            if watched_path == path or str(watched_path).startswith(str(path) + os.sep)
        ]

        for wd in wds_to_remove:
            with contextlib.suppress(Exception):
                self._inotify.rm_watch(wd)
            self._wd_to_info.pop(wd, None)

    def _on_inotify_events(self) -> None:
        """Handle inotify events (called by event loop when fd is readable)."""
        from inotify_simple import flags

        # Non-blocking read - we know there's data available
        events = self._inotify.read(timeout=0)

        for event in events:
            info = self._wd_to_info.get(event.wd)
            if info is None:
                continue

            watch_dir, callback = info

            # Map inotify flags to ChangeType
            if event.mask & flags.CREATE:
                change_type = ChangeType.CREATED
            elif event.mask & flags.DELETE:
                change_type = ChangeType.DELETED
            elif event.mask & flags.MODIFY:
                change_type = ChangeType.MODIFIED
            elif event.mask & (flags.MOVED_TO | flags.MOVED_FROM):
                change_type = ChangeType.RENAMED
            else:
                change_type = ChangeType.MODIFIED

            # Build path
            changed_path = str(watch_dir / event.name) if event.name else str(watch_dir)

            # Invoke callback
            change = FileChange(type=change_type, path=changed_path)
            try:
                callback(change)
            except Exception as e:
                logger.error(f"Error in file change callback: {e}")

    # =========================================================================
    # Windows Implementation (ReadDirectoryChangesW + RegisterWaitForSingleObject)
    # =========================================================================

    def _start_windows(self) -> None:
        """Initialize Windows watcher state."""
        self._watches = {}

    def _stop_windows(self) -> None:
        """Clean up all Windows watches."""
        for watch in list(self._watches.values()):
            watch.stop()
        self._watches.clear()

    def _add_watch_windows(self, path: Path, callback: FileChangeCallback, recursive: bool) -> None:
        """Add Windows directory watch with OS callback."""
        watch_path = path if path.is_dir() else path.parent
        key = str(watch_path)

        if key in self._watches:
            # Already watching this path
            return

        watch = _WindowsWatch(watch_path, callback, recursive, self._loop)
        watch.start()
        self._watches[key] = watch

    def _remove_watch_windows(self, path: Path) -> None:
        """Remove Windows directory watch."""
        watch_path = path if path.is_dir() else path.parent
        key = str(watch_path)

        watch = self._watches.pop(key, None)
        if watch:
            watch.stop()

    # =========================================================================
    # Backward Compatible One-Shot API
    # =========================================================================

    async def wait_for_change(
        self,
        path: str | Path,
        timeout: float = 30.0,
    ) -> FileChange | None:
        """Wait for a file system change on the given path (one-shot).

        This is a convenience method that creates a temporary watch,
        waits for one event, and returns it.

        Args:
            path: Path to watch (file or directory)
            timeout: Maximum time to wait in seconds (default: 30.0)

        Returns:
            FileChange if a change was detected, None if timeout

        Raises:
            NotImplementedError: If platform is not supported
            FileNotFoundError: If path does not exist
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        # Use event to signal when change is detected
        result: list[FileChange] = []
        event = asyncio.Event()

        def on_change(change: FileChange) -> None:
            if not result:  # Only capture first change
                result.append(change)
                # Schedule event.set() in the event loop
                if self._loop:
                    self._loop.call_soon_threadsafe(event.set)

        # Temporarily start if not already started
        was_started = self._started
        if not was_started:
            self.start()

        try:
            self.add_watch(path, on_change)
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
                return result[0] if result else None
            except TimeoutError:
                return None
            finally:
                self.remove_watch(path)
        finally:
            if not was_started:
                self.stop()

    def close(self) -> None:
        """Clean up any resources (alias for stop())."""
        self.stop()


# =============================================================================
# Windows Watch Implementation
# =============================================================================


class _WindowsWatch:
    """Manages a single Windows directory watch with OS-level callback.

    Uses RegisterWaitForSingleObject for true event-driven callbacks.
    """

    # Windows constants
    FILE_LIST_DIRECTORY = 1
    FILE_SHARE_READ = 1
    FILE_SHARE_WRITE = 2
    FILE_SHARE_DELETE = 4
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OVERLAPPED = 0x40000000
    FILE_NOTIFY_CHANGE_FILE_NAME = 0x01
    FILE_NOTIFY_CHANGE_DIR_NAME = 0x02
    FILE_NOTIFY_CHANGE_SIZE = 0x08
    FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
    INFINITE = 0xFFFFFFFF
    WT_EXECUTEDEFAULT = 0x00000000
    WT_EXECUTEONLYONCE = 0x00000008

    def __init__(
        self,
        path: Path,
        callback: FileChangeCallback,
        recursive: bool,
        loop: asyncio.AbstractEventLoop | None,
    ):
        self._path = path
        self._callback = callback
        self._recursive = recursive
        self._loop = loop
        self._running = False

        # Windows handles
        self._dir_handle: Any = None
        self._event_handle: Any = None
        self._wait_handle = ctypes.c_void_p()
        self._overlapped: Any = None
        self._buffer: Any = None

        # Action code mapping
        self._action_map = {
            1: ChangeType.CREATED,  # FILE_ACTION_ADDED
            2: ChangeType.DELETED,  # FILE_ACTION_REMOVED
            3: ChangeType.MODIFIED,  # FILE_ACTION_MODIFIED
            4: ChangeType.RENAMED,  # FILE_ACTION_RENAMED_OLD_NAME
            5: ChangeType.RENAMED,  # FILE_ACTION_RENAMED_NEW_NAME
        }

        # Keep reference to prevent GC
        self._wait_callback: Any = None

    def start(self) -> None:
        """Start watching the directory."""
        if self._running:
            return

        try:
            import pywintypes
            import win32event
            import win32file
        except ImportError as e:
            raise ImportError(
                "pywin32 is required for Windows file watching. Install with: pip install pywin32"
            ) from e

        # Open directory handle
        self._dir_handle = win32file.CreateFile(
            str(self._path),
            self.FILE_LIST_DIRECTORY,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE | self.FILE_SHARE_DELETE,
            None,
            self.OPEN_EXISTING,
            self.FILE_FLAG_BACKUP_SEMANTICS | self.FILE_FLAG_OVERLAPPED,
            None,
        )

        # Create event for overlapped I/O
        self._event_handle = win32event.CreateEvent(None, True, False, None)

        # Create overlapped structure
        self._overlapped = pywintypes.OVERLAPPED()
        self._overlapped.hEvent = self._event_handle

        # Allocate buffer
        self._buffer = win32file.AllocateReadBuffer(65536)

        # Start first async read
        self._start_read()

        # Register OS callback using ctypes
        self._register_wait_callback()

        self._running = True
        logger.debug(f"Windows watch started: {self._path}")

    def stop(self) -> None:
        """Stop watching and clean up resources."""
        if not self._running:
            return

        import win32api
        import win32file

        self._running = False

        # Unregister wait callback
        if self._wait_handle:
            try:
                kernel32 = ctypes.windll.kernel32
                kernel32.UnregisterWait(self._wait_handle)
            except Exception:
                pass
            self._wait_handle = ctypes.c_void_p()

        # Cancel pending I/O
        if self._dir_handle:
            with contextlib.suppress(Exception):
                win32file.CancelIo(self._dir_handle)

        # Close handles
        if self._event_handle:
            with contextlib.suppress(Exception):
                win32api.CloseHandle(self._event_handle)
            self._event_handle = None

        if self._dir_handle:
            with contextlib.suppress(Exception):
                win32api.CloseHandle(self._dir_handle)
            self._dir_handle = None

        logger.debug(f"Windows watch stopped: {self._path}")

    def _start_read(self) -> None:
        """Start an async ReadDirectoryChangesW operation."""
        import win32event
        import win32file

        # Reset event
        win32event.ResetEvent(self._event_handle)

        # Start async read
        watch_flags = (
            self.FILE_NOTIFY_CHANGE_FILE_NAME
            | self.FILE_NOTIFY_CHANGE_DIR_NAME
            | self.FILE_NOTIFY_CHANGE_SIZE
            | self.FILE_NOTIFY_CHANGE_LAST_WRITE
        )

        win32file.ReadDirectoryChangesW(
            self._dir_handle,
            self._buffer,
            self._recursive,
            watch_flags,
            self._overlapped,
        )

    def _register_wait_callback(self) -> None:
        """Register callback with Windows thread pool using RegisterWaitForSingleObject."""
        kernel32 = ctypes.windll.kernel32

        # Define callback type: VOID CALLBACK WaitOrTimerCallback(PVOID, BOOLEAN)
        WAITORTIMERCALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte)

        def wait_callback(_context: Any, timed_out: bool) -> None:
            """Called by Windows when event is signaled."""
            if timed_out or not self._running:
                return

            try:
                self._process_events()
            except Exception as e:
                logger.error(f"Error processing Windows file events: {e}")

            # Re-register for next event (if still running)
            if self._running:
                try:
                    self._start_read()
                    self._register_wait_callback()
                except Exception as e:
                    logger.error(f"Error re-registering Windows watch: {e}")

        # Keep reference to prevent garbage collection
        self._wait_callback = WAITORTIMERCALLBACK(wait_callback)

        # Get event handle as integer
        event_handle_int = int(self._event_handle)

        # Register wait
        result = kernel32.RegisterWaitForSingleObject(
            ctypes.byref(self._wait_handle),
            ctypes.c_void_p(event_handle_int),
            self._wait_callback,
            None,  # Context
            self.INFINITE,
            self.WT_EXECUTEONLYONCE,  # Execute once, we re-register after
        )

        if not result:
            error = ctypes.get_last_error()
            raise OSError(f"RegisterWaitForSingleObject failed: {error}")

    def _process_events(self) -> None:
        """Process completed ReadDirectoryChangesW results."""
        import win32file

        try:
            nbytes = win32file.GetOverlappedResult(self._dir_handle, self._overlapped, False)
        except Exception:
            return

        if not nbytes:
            return

        # Parse results
        results = win32file.FILE_NOTIFY_INFORMATION(self._buffer, nbytes)

        for action, filename in results:
            change_type = self._action_map.get(action, ChangeType.MODIFIED)
            full_path = str(self._path / filename)

            change = FileChange(type=change_type, path=full_path)

            # Invoke callback in event loop if available
            if self._loop and self._callback:  # type: ignore[truthy-function]
                self._loop.call_soon_threadsafe(self._callback, change)
            elif self._callback:  # type: ignore[truthy-function]
                try:
                    self._callback(change)
                except Exception as e:
                    logger.error(f"Error in file change callback: {e}")
