"""IsolatedPool — executor lifecycle, submission, and health management."""

from __future__ import annotations

import contextlib
import logging
import threading
from concurrent.futures import Executor, Future
from typing import Any

from nexus.isolation._compat import create_isolation_pool
from nexus.isolation._worker import worker_call, worker_get_property, worker_shutdown
from nexus.isolation.config import IsolationConfig
from nexus.isolation.errors import (
    IsolationCallError,
    IsolationPoolError,
    IsolationStartupError,
    IsolationTimeoutError,
)

logger = logging.getLogger(__name__)


class IsolatedPool:
    """Manages an executor pool for isolated backend calls.

    Thread-safe: all mutable state is guarded by ``_lock``.

    Responsibilities:
    - Lazy pool creation on first ``submit()``
    - Per-call timeout enforcement
    - Consecutive-failure tracking → automatic pool restart
    - Graceful shutdown with worker cleanup
    """

    def __init__(self, config: IsolationConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._pool: Executor | None = None
        self._consecutive_failures = 0
        self._shutdown = False

    # ── Public API ──────────────────────────────────────────────────────

    def submit(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Submit a backend method call to the pool.

        Raises
        ------
        IsolationPoolError
            If the pool has been shut down.
        IsolationTimeoutError
            If the call exceeds ``config.call_timeout``.
        IsolationCallError
            If the backend method raises an exception.
        IsolationStartupError
            If the backend cannot be imported / instantiated.
        """
        pool = self._ensure_pool()

        try:
            future: Future[Any] = pool.submit(
                worker_call,
                self._config.backend_module,
                self._config.backend_class,
                dict(self._config.backend_kwargs),
                method,
                args,
                kwargs,
            )
        except RuntimeError as exc:
            # Pool may have been restarted by another thread — retry once
            pool = self._ensure_pool()
            try:
                future = pool.submit(
                    worker_call,
                    self._config.backend_module,
                    self._config.backend_class,
                    dict(self._config.backend_kwargs),
                    method,
                    args,
                    kwargs,
                )
            except RuntimeError:
                self._record_failure()
                raise IsolationCallError(method, cause=exc) from exc

        try:
            result = future.result(timeout=self._config.call_timeout)
        except TimeoutError:
            future.cancel()  # best-effort; won't stop already-running work
            self._record_failure()
            raise IsolationTimeoutError(method, self._config.call_timeout) from None
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            self._record_failure()
            raise IsolationStartupError(
                self._config.backend_module, self._config.backend_class, cause=exc
            ) from exc
        except BaseException as exc:
            # BaseException covers SystemExit / KeyboardInterrupt from crashed workers
            self._record_failure()
            raise IsolationCallError(method, cause=exc) from exc
        else:
            self._record_success()
            return result

    def get_property(self, prop: str) -> Any:
        """Read a backend property in the worker.

        Uses ``startup_timeout`` since this typically happens during init.
        """
        pool = self._ensure_pool()

        try:
            future: Future[Any] = pool.submit(
                worker_get_property,
                self._config.backend_module,
                self._config.backend_class,
                dict(self._config.backend_kwargs),
                prop,
            )
        except RuntimeError as exc:
            pool = self._ensure_pool()
            try:
                future = pool.submit(
                    worker_get_property,
                    self._config.backend_module,
                    self._config.backend_class,
                    dict(self._config.backend_kwargs),
                    prop,
                )
            except RuntimeError:
                raise IsolationCallError(f"property:{prop}", cause=exc) from exc

        try:
            result = future.result(timeout=self._config.startup_timeout)
        except TimeoutError:
            raise IsolationTimeoutError(f"property:{prop}", self._config.startup_timeout) from None
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            raise IsolationStartupError(
                self._config.backend_module, self._config.backend_class, cause=exc
            ) from exc
        except BaseException as exc:
            raise IsolationCallError(f"property:{prop}", cause=exc) from exc
        else:
            return result

    def shutdown(self) -> None:
        """Shut down the pool and disconnect workers."""
        with self._lock:
            self._shutdown = True
            pool = self._pool
            self._pool = None
        if pool is not None:
            self._try_shutdown_workers(pool)
            pool.shutdown(wait=True)

    @property
    def is_alive(self) -> bool:
        return not self._shutdown

    # ── Internal ────────────────────────────────────────────────────────

    def _ensure_pool(self) -> Executor:
        with self._lock:
            if self._shutdown:
                raise IsolationPoolError("pool has been shut down")
            if self._pool is None:
                self._pool = create_isolation_pool(
                    self._config.pool_size,
                    force_process=self._config.force_process,
                )
            return self._pool

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.max_consecutive_failures:
                logger.warning(
                    "Isolation pool hit %d consecutive failures — restarting",
                    self._consecutive_failures,
                )
                self._restart_pool_locked()

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def _restart_pool_locked(self) -> None:
        """Replace the pool (must be called with ``_lock`` held).

        The old pool is cleaned up in a background daemon thread to avoid
        blocking the lock while workers disconnect and processes terminate.
        """
        old_pool = self._pool
        self._pool = None
        self._consecutive_failures = 0
        if old_pool is not None:
            threading.Thread(
                target=self._cleanup_old_pool,
                args=(old_pool,),
                daemon=True,
                name="isolation-pool-cleanup",
            ).start()

    def _cleanup_old_pool(self, pool: Executor) -> None:
        """Best-effort cleanup of a replaced pool (runs in background thread)."""
        self._try_shutdown_workers(pool)
        try:
            pool.shutdown(wait=True)
        except Exception:
            logger.debug("Failed to shut down old pool during restart", exc_info=True)

    def _try_shutdown_workers(self, pool: Executor) -> None:
        """Best-effort worker cleanup — disconnect backend instances in all workers."""
        futures = []
        for _ in range(self._config.pool_size):
            try:
                futures.append(pool.submit(worker_shutdown))
            except Exception:
                break
        for f in futures:
            with contextlib.suppress(Exception):
                f.result(timeout=5.0)
