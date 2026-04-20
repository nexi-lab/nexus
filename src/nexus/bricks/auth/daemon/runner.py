"""Daemon runner: coordinates watcher + JWT renewal + status file + shutdown (#3804).

This module has no business logic; it wires the other daemon pieces together
and arbitrates lifecycle (start, run, signal-driven shutdown). All work is
delegated to the watcher, the pusher, and an optional JWT refresh callable.
"""

from __future__ import annotations

import base64
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


def _codex_account_from_auth_json(content: bytes) -> str | None:
    """Extract an account identifier from a codex auth.json blob.

    Codex stores its credentials as::

        {
          "tokens": {
            "id_token": "<JWT with `email` claim>",
            ...
          },
          ...
        }

    We parse the id_token's payload (signature NOT verified — we trust the
    local file, we only want to label the account) and return the first
    available identity claim: ``email`` → ``preferred_username`` → ``sub``.
    Returns ``None`` on any parse failure so the caller can skip rather
    than push under a shared wildcard. Never raises.
    """
    try:
        doc = json.loads(content.decode("utf-8"))
    except Exception:
        return None
    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if not isinstance(id_token, str):
        # Some codex setups only have an API key with no OIDC id_token.
        # Fall back to an explicit account_id / email at the top level.
        for key in ("account_id", "email", "preferred_username", "sub"):
            v = doc.get(key) if isinstance(doc, dict) else None
            if isinstance(v, str) and v:
                return v
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    pad = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("email", "preferred_username", "sub"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    return None


class _Pusher(Protocol):
    def push_source(
        self,
        source: str,
        *,
        content: bytes,
        provider: str | None = None,
        account_identifier: str,
    ) -> None: ...


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
        retry_pending_every: int = 60,
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
        # Periodic retry of pending queue rows — runs continuously so a
        # transient outage doesn't strand queued writes until the next file
        # change. The watched file is re-read + re-pushed, so hash-matching
        # pending rows clear on the first successful retry.
        self._retry_pending_every = retry_pending_every

        self._stop = threading.Event()
        self._state: str = "healthy"
        self._last_success_at: str | None = None

        self._watcher: SourceWatcher | None = None
        self._jwt_thread: threading.Thread | None = None
        self._subprocess_thread: threading.Thread | None = None
        self._retry_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API

    def status(self) -> DaemonStatus:
        """Current snapshot; safe to call at any point in the lifecycle."""
        return DaemonStatus(
            state=self._state,
            last_success_at=self._last_success_at,
            dirty_rows=len(self._queue.list_pending()),
        )

    def drain_startup(self) -> None:
        """Replay the watched source IFF the queue has pending rows.

        Gating on pending is important: a blind re-read on every startup
        would re-push unchanged local state and could clobber newer central
        state in multi-writer scenarios (or just generate audit churn). When
        the queue is empty we trust the steady-state watcher to pick up any
        future change and skip the replay.

        When pending rows exist we re-read the watched file and route it
        through ``_on_change`` — if content still matches the pending hash
        the push succeeds and the queue row clears; if it has changed, the
        new push supersedes the stale entry via server-side last-write-wins.
        Payload-durable queue replay (replay the EXACT bytes of the failed
        push) is tracked as follow-up; MVP trusts the source-of-truth file.
        """
        pending = self._queue.list_pending()
        log.info("startup drain: %d pending rows", len(pending))
        if not pending:
            return
        watch_path = self._source_watch_target
        if not watch_path.exists():
            log.debug("startup replay: watch target %s does not exist", watch_path)
            return
        try:
            content = watch_path.read_bytes()
        except OSError:
            log.warning("startup replay: failed to read %s", watch_path)
            return
        if not content:
            return
        # Route through the normal _on_change path so account-extraction +
        # degrade-on-failure semantics stay consistent with live operation.
        self._on_change(watch_path, content)

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

        if self._retry_pending_every > 0:
            rt = threading.Thread(
                target=self._retry_pending_loop,
                name="daemon-retry-pending",
                daemon=True,
            )
            rt.start()
            self._retry_thread = rt

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
        account = _codex_account_from_auth_json(content)
        if account is None:
            # Refuse to push under a wildcard — it would silently overwrite
            # other accounts that share "codex/unknown" as their profile_id.
            log.warning(
                "codex push skipped: could not extract account_identifier "
                "from auth.json (tokens.id_token missing or undecodable)"
            )
            self._maybe_degrade()
            return
        try:
            self._pusher.push_source(
                "codex",
                content=content,
                provider="codex",
                account_identifier=account,
            )
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

    def _retry_pending_loop(self) -> None:
        """Periodically replay pending queue rows until the queue is empty.

        The queue only stores ``(profile_id, payload_hash, attempts)`` — it
        has no envelope payload to re-submit directly. Instead we re-read
        every source we know about (watched file + subprocess adapters) on a
        fixed cadence, so the existing push+dedupe pipeline clears matching
        pending rows on success. This closes the “network blip strands the
        queue until the next file change” gap flagged in review.

        Light backoff: sleep ``retry_pending_every`` seconds between cycles
        (already a multiple of the watcher debounce). No exponential growth
        — we cap at the fixed interval to keep bookkeeping simple for a
        small pending-row count.
        """
        while not self._stop.is_set():
            if self._stop.wait(timeout=float(self._retry_pending_every)):
                return
            try:
                pending = self._queue.list_pending()
            except Exception:
                log.exception("retry loop: list_pending failed")
                continue
            if not pending:
                continue
            log.info("retry loop: %d pending rows — replaying known sources", len(pending))
            self._replay_known_sources()

    def _replay_known_sources(self) -> None:
        """Re-read + re-push the watched file and every subprocess source.

        Used by the retry loop; shares the drain-startup semantics so both
        paths keep identical push behavior (account extraction, skip-on-
        missing, degrade-on-failure). Swallows exceptions — we don't want
        retry bookkeeping to kill the daemon.
        """
        watch_path = self._source_watch_target
        if watch_path.exists():
            try:
                content = watch_path.read_bytes()
            except OSError:
                log.warning("retry loop: failed to read %s", watch_path)
                content = b""
            if content:
                try:
                    self._on_change(watch_path, content)
                except Exception:
                    log.exception("retry loop: watched source replay raised")
        if self._subprocess_sources:
            try:
                self._fetch_all_subprocess_sources()
            except Exception:
                log.exception("retry loop: subprocess replay raised")

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
            account = src.fetch_account_label()
            if not account:
                # Without a stable account label, multiple enrolled accounts
                # would collide on the same profile_id. Skip and warn instead
                # of merging them under a shared wildcard.
                log.warning(
                    "subprocess push skipped: adapter %s has no account label "
                    "(account_cmd missing or failed)",
                    src.name,
                )
                continue
            try:
                self._pusher.push_source(
                    src.name,
                    content=content,
                    account_identifier=account,
                )
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
