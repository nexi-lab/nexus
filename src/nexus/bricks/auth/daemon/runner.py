"""Daemon runner: coordinates watcher + JWT renewal + status file + shutdown (#3804).

This module has no business logic; it wires the other daemon pieces together
and arbitrates lifecycle (start, run, signal-driven shutdown). All work is
delegated to the watcher, the pusher, and an optional JWT refresh callable.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from nexus.bricks.auth.daemon.queue import PushQueue
from nexus.bricks.auth.daemon.watcher import SourceWatcher

log = logging.getLogger(__name__)


class _Pusher(Protocol):
    def push_source(self, source: str, *, content: bytes, provider: str | None = None) -> None: ...


@dataclass
class DaemonStatus:
    """Serializable snapshot of the daemon's health."""

    state: str  # "healthy" | "degraded" | "stopped"
    last_success_at: str | None
    dirty_rows: int

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class DaemonRunner:
    """Orchestrates watcher + JWT renewal + status file + signal-driven shutdown.

    Thread-safety notes:

    - ``self._stop`` is a ``threading.Event``; ``wait(timeout=...)`` gives the
      idle loop a fast-exit path when ``shutdown()`` is called.
    - ``self._state`` is a plain ``str``; reassignment is atomic in CPython.
    - ``_on_change`` swallows all exceptions (degraded mode) so a single bad
      push never tears down the watcher callback chain.
    """

    def __init__(
        self,
        *,
        source_watch_target: Path,
        queue: PushQueue,
        pusher: _Pusher,  # duck-typed: push_source(source, *, content, provider)
        jwt_refresh_every: int,
        status_path: Path,
        jwt_refresh_callable: Callable[[], None] | None = None,
    ) -> None:
        self._source_watch_target = source_watch_target
        self._queue = queue
        self._pusher = pusher
        self._jwt_refresh_every = jwt_refresh_every
        self._status_path = status_path
        self._jwt_refresh_callable = jwt_refresh_callable

        self._stop = threading.Event()
        self._state: str = "healthy"
        self._last_success_at: str | None = None

        self._watcher: SourceWatcher | None = None
        self._jwt_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API

    def status(self) -> DaemonStatus:
        """Current snapshot; safe to call at any point in the lifecycle."""
        return DaemonStatus(
            state=self._state,
            last_success_at=self._last_success_at,
            dirty_rows=len(self._queue.list_pending()),
        )

    def drain_startup(self) -> None:
        """MVP: just log the count of pending rows.

        The next watcher fire will trigger ``push_source`` which already knows
        how to flush dedupe state; we intentionally don't re-push from here to
        avoid doubling up on the watcher's debounce flow.
        """
        pending = self._queue.list_pending()
        log.info("startup drain: %d pending rows", len(pending))

    def run(self) -> None:
        """Block until ``shutdown()`` fires (or a signal triggers it)."""
        self._install_signal_handlers()

        self.drain_startup()

        watcher = SourceWatcher(
            self._source_watch_target,
            on_change=self._on_change,
            debounce_ms=500,
        )
        watcher.start()
        self._watcher = watcher

        if self._jwt_refresh_callable is not None:
            t = threading.Thread(
                target=self._jwt_refresh_loop,
                name="daemon-jwt-refresh",
                daemon=True,
            )
            t.start()
            self._jwt_thread = t

        try:
            while not self._stop.is_set():
                self._write_status()
                # Short wait — exits fast on shutdown.
                self._stop.wait(timeout=5.0)
        finally:
            self._finalize()

    def shutdown(self) -> None:
        """Signal the run-loop to exit. Idempotent."""
        self._stop.set()

    # -------------------------------------------------------------- internals

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM + SIGINT → shutdown.

        Signal handlers can only be installed in the main thread. When
        ``run()`` is invoked from a sub-thread (tests do this), ``signal.signal``
        raises ``ValueError``; we swallow it and rely on explicit
        ``shutdown()`` calls instead.
        """
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except ValueError:
            log.debug("signal handlers not installed (not main thread)")

    def _on_signal(self, _signum: int, _frame: object) -> None:
        self.shutdown()

    def _on_change(self, _path: Path, content: bytes) -> None:
        try:
            self._pusher.push_source("codex", content=content, provider="codex")
        except Exception:
            log.exception("push_source failed")
            self._maybe_degrade()
        else:
            self._mark_success()

    def _mark_success(self) -> None:
        self._state = "healthy"
        self._last_success_at = datetime.now(UTC).isoformat()

    def _maybe_degrade(self) -> None:
        # MVP: any failure → degraded. Follow-up can require sustained failures.
        self._state = "degraded"

    def _jwt_refresh_loop(self) -> None:
        assert self._jwt_refresh_callable is not None
        while not self._stop.is_set():
            # Wait first so we don't race the caller's own startup refresh.
            if self._stop.wait(timeout=float(self._jwt_refresh_every)):
                return
            try:
                self._jwt_refresh_callable()
            except Exception:
                log.exception("jwt refresh failed")
                self._maybe_degrade()

    def _write_status(self) -> None:
        """Best-effort atomic write of the status file."""
        status = self.status()
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
            tmp.write_text(status.to_json())
            os.replace(tmp, self._status_path)
        except OSError:
            log.exception("failed to write status file")

    def _finalize(self) -> None:
        """Stop watcher, mark stopped, write one last status."""
        watcher = self._watcher
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                log.exception("watcher stop raised")
            self._watcher = None

        self._state = "stopped"
        self._write_status()
