"""Cross-platform file watcher using watchfiles (Rust-backed).

This module provides OS-native file watching using the ``watchfiles`` library,
which wraps the Rust ``notify`` crate.  Supports Linux (inotify), macOS
(FSEvents), and Windows (ReadDirectoryChangesW) — all through a single
async API with built-in debouncing.

Architecture:
- FileWatcher: Persistent watcher with callback registration
- add_watch(path, callback): Register callback for path changes
- wait_for_change(path, timeout): One-shot convenience method
- Events are delivered via callbacks, no polling required

Used by NexusFS for:
1. wait_for_changes() API — user-facing file change notifications
2. Cache invalidation — automatic cache updates on external changes
"""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from watchfiles import Change, awatch

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CHANGE_MAP: dict[Change, ChangeType] = {
    Change.added: ChangeType.CREATED,
    Change.modified: ChangeType.MODIFIED,
    Change.deleted: ChangeType.DELETED,
}


@dataclass
class _WatchInfo:
    """Tracks a single registered watch (path + callback + background task)."""

    path: Path
    callback: FileChangeCallback
    recursive: bool
    task: asyncio.Task[None] | None = None


class _RenameDetector:
    """Correlate delete+add pairs in the same directory into RENAMED events.

    ``watchfiles`` does not emit a dedicated rename event.  Instead, an
    OS-level rename appears as ``(deleted, old_path)`` followed by
    ``(added, new_path)``.  When a batch contains exactly one delete and
    one add in the *same directory*, we collapse them into a single
    ``RENAMED`` event.

    False-positives (independent delete + add in the same batch) are rare
    and harmless — the consumer sees a rename where there was really a
    delete-then-create, which is semantically equivalent.
    """

    @staticmethod
    def _is_hidden(path_str: str) -> bool:
        """Check if a path refers to a hidden/system file (e.g. .DS_Store)."""
        return Path(path_str).name.startswith(".")

    @staticmethod
    def process(raw: set[tuple[Change, str]]) -> list[FileChange]:
        added: list[str] = []
        deleted: list[str] = []
        others: list[FileChange] = []

        for change, path_str in raw:
            if change == Change.added:
                added.append(path_str)
            elif change == Change.deleted:
                deleted.append(path_str)
            else:
                ct = _CHANGE_MAP.get(change, ChangeType.MODIFIED)
                others.append(FileChange(type=ct, path=path_str))

        # Filter out hidden/system files (e.g. .DS_Store) for rename detection
        visible_added = [p for p in added if not _RenameDetector._is_hidden(p)]
        visible_deleted = [p for p in deleted if not _RenameDetector._is_hidden(p)]

        # Check for rename: 1 visible delete + 1 visible add in same directory
        if (
            len(visible_deleted) == 1
            and len(visible_added) == 1
            and str(Path(visible_deleted[0]).parent) == str(Path(visible_added[0]).parent)
        ):
            # Emit rename for the visible pair
            result: list[FileChange] = [
                FileChange(
                    type=ChangeType.RENAMED,
                    path=visible_added[0],
                    old_path=visible_deleted[0],
                ),
            ]
            # Emit remaining hidden files as individual events
            for p in added:
                if p != visible_added[0]:
                    result.append(FileChange(type=ChangeType.CREATED, path=p))
            for p in deleted:
                if p != visible_deleted[0]:
                    result.append(FileChange(type=ChangeType.DELETED, path=p))
            result.extend(others)
            return result

        # No rename detected — emit individual events
        result_list: list[FileChange] = []
        for p in deleted:
            result_list.append(FileChange(type=ChangeType.DELETED, path=p))
        for p in added:
            result_list.append(FileChange(type=ChangeType.CREATED, path=p))
        result_list.extend(others)
        return result_list


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class FileWatcher:
    """Cross-platform file watcher using watchfiles (Rust-backed).

    Uses the ``watchfiles`` library which wraps Rust's ``notify`` crate for
    true OS-native, event-driven file watching on Linux, macOS, and Windows.

    Example (callback mode):
        >>> watcher = FileWatcher()
        >>> watcher.start()
        >>> watcher.add_watch("/inbox", lambda change: print(f"Changed: {change.path}"))
        >>> # ... events delivered via callbacks ...
        >>> watcher.stop()

    Example (one-shot mode):
        >>> watcher = FileWatcher()
        >>> change = await watcher.wait_for_change("/inbox", timeout=30.0)
    """

    _started: bool = field(default=False, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _watches: dict[str, _WatchInfo] = field(default_factory=dict, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _force_polling: bool = field(default=False, init=False, repr=False)

    # -- lifecycle -----------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the file watcher.

        Must be called before add_watch(). Initialises internal state and
        records the event loop to use for background tasks.

        Args:
            loop: Event loop to use (defaults to current running loop).
        """
        if self._started:
            return

        self._loop = loop or asyncio.get_running_loop()
        self._stop_event.clear()
        self._force_polling = os.environ.get("NEXUS_FILE_WATCHER_POLL", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self._started = True
        logger.info("FileWatcher started")

    def stop(self) -> None:
        """Stop the file watcher and release all resources."""
        if not self._started:
            return

        self._stop_event.set()

        for info in self._watches.values():
            if info.task is not None and not info.task.done():
                info.task.cancel()
        self._watches.clear()

        self._started = False
        self._loop = None
        logger.info("FileWatcher stopped")

    # -- persistent watches --------------------------------------------------

    def add_watch(
        self,
        path: str | Path,
        callback: FileChangeCallback,
        recursive: bool = True,
    ) -> None:
        """Add a watch for file system changes on the given path.

        Args:
            path: Path to watch (file or directory).
            callback: Function called when changes are detected.
            recursive: Watch subdirectories recursively (default: True).

        Raises:
            RuntimeError: If watcher not started.
            FileNotFoundError: If path does not exist.
        """
        if not self._started:
            raise RuntimeError("FileWatcher not started. Call start() first.")

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        key = str(path)
        if key in self._watches:
            return  # already watching

        info = _WatchInfo(path=path, callback=callback, recursive=recursive)

        # Determine the actual path to watch with watchfiles
        watch_path = path if path.is_dir() else path.parent

        assert self._loop is not None  # guaranteed by _started check above
        info.task = self._loop.create_task(
            self._watch_loop(watch_path, callback, recursive),
        )
        self._watches[key] = info
        logger.debug("Added watch: %s (recursive=%s)", path, recursive)

    def remove_watch(self, path: str | Path) -> None:
        """Remove a watch for the given path.

        Args:
            path: Path to stop watching.
        """
        if not self._started:
            return

        key = str(Path(path))
        info = self._watches.pop(key, None)
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
        logger.debug("Removed watch: %s", path)

    # -- one-shot API --------------------------------------------------------

    async def wait_for_change(
        self,
        path: str | Path,
        timeout: float = 30.0,
    ) -> FileChange | None:
        """Wait for a file system change on the given path (one-shot).

        Creates a temporary watch, waits for the first event, then cleans up.

        Args:
            path: Path to watch (file or directory).
            timeout: Maximum time to wait in seconds (default: 30.0).

        Returns:
            FileChange if a change was detected, None on timeout.

        Raises:
            FileNotFoundError: If path does not exist.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        watch_path = path if path.is_dir() else path.parent

        stop = asyncio.Event()

        try:
            async with asyncio.timeout(timeout):
                async for changes in awatch(
                    watch_path,
                    recursive=True,
                    step=50,  # 50 ms debounce for responsive one-shot
                    stop_event=stop,
                    force_polling=self._force_polling,
                ):
                    file_changes = _RenameDetector.process(changes)
                    if file_changes:
                        return file_changes[0]
        except TimeoutError:
            return None
        finally:
            stop.set()

        return None  # pragma: no cover — unreachable but satisfies mypy

    def close(self) -> None:
        """Clean up any resources (alias for stop())."""
        self.stop()

    # -- internal ------------------------------------------------------------

    async def _watch_loop(
        self,
        watch_path: Path,
        callback: FileChangeCallback,
        recursive: bool,
    ) -> None:
        """Run ``awatch()`` in a loop, invoking *callback* for each batch."""
        try:
            async for changes in awatch(
                watch_path,
                recursive=recursive,
                step=100,  # 100 ms debounce for persistent watches
                stop_event=self._stop_event,
                force_polling=self._force_polling,
            ):
                file_changes = _RenameDetector.process(changes)
                for fc in file_changes:
                    try:
                        callback(fc)
                    except Exception:
                        logger.exception(
                            "Error in file change callback for %s (type=%s)",
                            fc.path,
                            fc.type.value,
                        )
        except asyncio.CancelledError:
            pass  # normal shutdown
        except Exception:
            logger.exception("Unexpected error in watch loop for %s", watch_path)
