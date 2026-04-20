"""Daemon runner: coordinates watcher + JWT renewal + status file + shutdown (#3804).

This module has no business logic; it wires the other daemon pieces together
and arbitrates lifecycle (start, run, signal-driven shutdown). All work is
delegated to the watcher, the pusher, and an optional JWT refresh callable.
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from nexus.bricks.auth.daemon.adapters import SubprocessSource
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
        jwt_expiry_provider: Callable[[], float | None] | None = None,
        jwt_refresh_margin_s: int = 60,
        subprocess_sources: tuple[SubprocessSource, ...] = (),
        subprocess_poll_every: int = 300,
    ) -> None:
        self._source_watch_target = source_watch_target
        self._queue = queue
        self._pusher = pusher
        self._jwt_refresh_every = jwt_refresh_every
        self._status_path = status_path
        self._jwt_refresh_callable = jwt_refresh_callable
        # Optional: returns seconds-until-expiry for the cached token, used to
        # derive refresh timing from the actual token ``exp`` rather than a
        # fixed interval (#3804 review feedback). ``None`` → fixed interval.
        self._jwt_expiry_provider = jwt_expiry_provider
        self._jwt_refresh_margin_s = jwt_refresh_margin_s
        self._subprocess_sources = subprocess_sources
        self._subprocess_poll_every = subprocess_poll_every

        self._stop = threading.Event()
        self._state: str = "healthy"
        self._last_success_at: str | None = None

        self._watcher: SourceWatcher | None = None
        self._jwt_thread: threading.Thread | None = None
        self._subprocess_thread: threading.Thread | None = None

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

        if self._subprocess_sources:
            sp = threading.Thread(
                target=self._subprocess_poll_loop,
                name="daemon-subprocess-poll",
                daemon=True,
            )
            sp.start()
            self._subprocess_thread = sp

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
        """Refresh JWT proactively, guided by the token's own ``exp`` claim.

        When ``jwt_expiry_provider`` is set, the loop sleeps until
        ``exp - margin`` (with ±10% jitter), so a token with a 1-hour TTL
        refreshes well before a 45-minute fixed cadence would. Fallback
        behavior (no provider, or ``None`` return) is the legacy fixed
        ``jwt_refresh_every`` cadence with ±10% jitter. Jitter spreads
        herd-after-restart (#3788) — at 100k enrolled daemons that keeps
        refresh bursts inside a single uvicorn worker's capacity.
        """
        assert self._jwt_refresh_callable is not None
        while not self._stop.is_set():
            wait_s = self._next_refresh_wait_s()
            # Wait first so we don't race the caller's own startup refresh.
            if self._stop.wait(timeout=wait_s):
                return
            try:
                self._jwt_refresh_callable()
            except Exception:
                log.exception("jwt refresh failed")
                self._maybe_degrade()

    def _next_refresh_wait_s(self) -> float:
        """Compute the next refresh interval; exposed for easier testing."""
        base = float(self._jwt_refresh_every)
        if self._jwt_expiry_provider is not None:
            try:
                remaining = self._jwt_expiry_provider()
            except Exception:
                remaining = None
            if remaining is not None:
                proactive = float(remaining) - float(self._jwt_refresh_margin_s)
                if proactive < base:
                    # Floor at 60s so even a near-expired token doesn't thrash.
                    base = max(60.0, proactive)
        jitter = random.uniform(-0.1, 0.1) * base
        return max(1.0, base + jitter)

    def _subprocess_poll_loop(self) -> None:
        """Fetch each SubprocessSource on an interval; hand bytes to pusher.

        Pusher's per-source hash dedupe suppresses no-op writes, so polling
        a stable token every 5 min is effectively free on the server side.
        """
        # Immediate first fetch so first-run can push without waiting a cycle.
        self._fetch_all_subprocess_sources()
        while not self._stop.is_set():
            if self._stop.wait(timeout=float(self._subprocess_poll_every)):
                return
            self._fetch_all_subprocess_sources()

    def _fetch_all_subprocess_sources(self) -> None:
        for src in self._subprocess_sources:
            content = src.fetch()
            if content is None:
                continue
            try:
                self._pusher.push_source(src.name, content=content)
            except Exception:
                log.exception("subprocess push failed name=%s", src.name)
                self._maybe_degrade()
            else:
                self._mark_success()

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
