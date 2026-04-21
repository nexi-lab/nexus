"""Debounced fsnotify watcher for a single target file (#3804)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
    FileSystemMovedEvent,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

log = logging.getLogger(__name__)


class SourceWatcher:
    """Watch a single target file; debounce rapid writes into one callback.

    watchdog fires events at the directory level, so we watch the target's
    parent and filter events to those matching the resolved target path.
    """

    def __init__(
        self,
        target: Path,
        *,
        on_change: Callable[[Path, bytes], None],
        debounce_ms: int = 500,
    ) -> None:
        self._target = target
        self._on_change = on_change
        self._debounce_s = debounce_ms / 1000.0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._observer: BaseObserver | None = None

    def start(self) -> None:
        parent = self._target.parent
        parent.mkdir(parents=True, exist_ok=True)

        handler = _TargetHandler(self._target, self._schedule)
        observer = Observer()
        observer.schedule(handler, str(parent), recursive=False)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        observer = self._observer
        if observer is not None:
            observer.stop()
            observer.join(timeout=2.0)
            self._observer = None
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._debounce_s, self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            content = self._target.read_bytes()
        except FileNotFoundError:
            log.info("watcher: target disappeared, ignoring")
            return
        except OSError:
            log.exception("watcher: failed to read target")
            return
        try:
            self._on_change(self._target, content)
        except Exception:
            log.exception("watcher: on_change raised")


class _TargetHandler(FileSystemEventHandler):
    def __init__(self, target: Path, schedule: Callable[[], None]) -> None:
        super().__init__()
        self._target = target
        self._schedule = schedule

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Trigger when an atomic temp-file rename lands at the target path.

        The canonical safe way to update a credential file is to write a
        tmp file then ``rename`` it over the target. Under ``watchdog`` that
        surfaces as ``FileSystemMovedEvent`` with ``dest_path`` set to the
        target — neither ``on_modified`` nor ``on_created`` fires. Without
        handling this, the daemon silently misses credential rotations and
        central state goes stale.
        """
        # Prefer dest_path (where the file landed); fall back to src_path
        # for unusual backends that only populate src_path.
        dest_raw = getattr(event, "dest_path", None) or event.src_path
        dest_path = dest_raw.decode() if isinstance(dest_raw, bytes) else dest_raw
        if self._path_matches_target(dest_path, event):
            self._schedule()
            return
        # Also handle the rename-AWAY case: if the target was moved out of
        # the way, a rescheduling callback lets ``_fire`` discover the new
        # content (if any other writer replaces it) without waiting for a
        # subsequent modify event that may never come.
        src_raw = getattr(event, "src_path", None)
        src_path = src_raw.decode() if isinstance(src_raw, bytes) else src_raw
        if self._path_matches_target(src_path, event):
            self._schedule()

    def _maybe_schedule(self, event: FileSystemEvent) -> None:
        src_path = getattr(event, "src_path", None)
        if self._path_matches_target(src_path, event):
            self._schedule()

    def _path_matches_target(self, candidate: str | None, event: FileSystemEvent) -> bool:
        if getattr(event, "is_directory", False):
            return False
        if not candidate:
            return False
        try:
            resolved = Path(candidate).resolve()
            target_resolved = self._target.resolve()
        except OSError:
            return False
        return resolved == target_resolved


# Re-export for backwards compat / narrow type imports in tests.
__all__ = ["SourceWatcher", "FileSystemMovedEvent"]
