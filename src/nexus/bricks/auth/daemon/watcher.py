"""Debounced fsnotify watcher for a single target file (#3804)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
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

    def _maybe_schedule(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        src_path = getattr(event, "src_path", None)
        if src_path is None:
            return
        try:
            resolved = Path(src_path).resolve()
            target_resolved = self._target.resolve()
        except OSError:
            return
        if resolved != target_resolved:
            return
        self._schedule()
