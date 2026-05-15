"""ExporterRegistry — manages and dispatches to registered exporters.

Dispatches event batches to all registered exporters in parallel via
asyncio.gather(). Per-exporter circuit breaker and timeout prevent one
slow/dead exporter from blocking delivery to healthy exporters.

Issue #1138: Event Stream Export.
Issue #2750: Per-exporter circuit breaker and timeout.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from nexus.lib.circuit_breaker import CircuitBreakerBase, CircuitState

if TYPE_CHECKING:
    from nexus.services.event_bus.types import FileEvent
    from nexus.services.event_log.exporter_protocol import (
        EventStreamExporterProtocol,
    )

logger = logging.getLogger(__name__)

# Defaults matching issue #2750 acceptance criteria
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_RESET_TIMEOUT = 60.0
_DEFAULT_EXPORTER_TIMEOUT = 30.0


class ExporterRegistry:
    """Registry of event stream exporters with parallel dispatch.

    Each exporter gets its own circuit breaker (trip after 3 consecutive
    failures, auto-reset after 60s) and a per-dispatch timeout (default
    30s).  A tripped breaker fast-fails without attempting the network
    call, returning all event IDs as failed for DLQ routing.

    Issue #2750.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        reset_timeout: float = _DEFAULT_RESET_TIMEOUT,
        exporter_timeout: float = _DEFAULT_EXPORTER_TIMEOUT,
    ) -> None:
        self._exporters: dict[str, EventStreamExporterProtocol] = {}
        self._breakers: dict[str, CircuitBreakerBase] = {}
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._exporter_timeout = exporter_timeout

    def register(self, exporter: "EventStreamExporterProtocol") -> None:
        """Register an exporter. Overwrites if name already exists."""
        self._exporters[exporter.name] = exporter
        self._breakers[exporter.name] = CircuitBreakerBase(
            failure_threshold=self._failure_threshold,
            success_threshold=1,
            reset_timeout=self._reset_timeout,
        )
        logger.info("Registered event exporter: %s", exporter.name)

    def unregister(self, name: str) -> None:
        """Remove an exporter by name."""
        removed = self._exporters.pop(name, None)
        self._breakers.pop(name, None)
        if removed:
            logger.info("Unregistered event exporter: %s", name)

    @property
    def exporter_names(self) -> list[str]:
        return list(self._exporters)

    @property
    def breaker_states(self) -> dict[str, str]:
        """Return circuit breaker state for each exporter (observability)."""
        return {name: breaker.current_state.value for name, breaker in self._breakers.items()}

    async def dispatch_batch(self, events: "list[FileEvent]") -> dict[str, list[str]]:
        """Dispatch a batch of events to all registered exporters in parallel.

        Per-exporter circuit breaker and timeout ensure one slow/dead
        exporter does not block delivery to other healthy exporters.

        Returns:
            Dict mapping exporter_name → list of failed event_ids.
            Empty dict values mean all events succeeded for that exporter.
        """
        if not self._exporters or not events:
            return {}

        results = await asyncio.gather(
            *[self._dispatch_one(name, exp, events) for name, exp in self._exporters.items()]
        )
        return {name: failed for name, failed in results if failed}

    async def _dispatch_one(
        self,
        name: str,
        exporter: "EventStreamExporterProtocol",
        events: "list[FileEvent]",
    ) -> tuple[str, list[str]]:
        """Dispatch to a single exporter with circuit breaker + timeout."""
        breaker = self._breakers.get(name)
        all_ids = [e.event_id for e in events]

        # Fast-fail if circuit is OPEN
        if breaker and breaker.current_state is CircuitState.OPEN:
            logger.warning(
                "Exporter %s circuit OPEN — fast-failing %d events to DLQ",
                name,
                len(events),
            )
            return (name, all_ids)

        try:
            async with asyncio.timeout(self._exporter_timeout):
                failed_ids = await exporter.publish_batch(events)

            # Record outcome on breaker
            if breaker:
                if failed_ids:
                    await breaker._record_failure()
                else:
                    await breaker._record_success()

            return (name, failed_ids)

        except TimeoutError:
            logger.error(
                "Exporter %s timed out after %.1fs dispatching %d events",
                name,
                self._exporter_timeout,
                len(events),
            )
            if breaker:
                await breaker._record_failure()
            return (name, all_ids)

        except Exception:
            logger.exception("Exporter %s batch dispatch failed", name)
            if breaker:
                await breaker._record_failure()
            return (name, all_ids)

    async def close_all(self) -> None:
        """Gracefully close all registered exporters."""
        for name, exporter in self._exporters.items():
            try:
                await exporter.close()
                logger.info("Closed exporter: %s", name)
            except Exception:
                logger.exception("Error closing exporter: %s", name)
        self._exporters.clear()
        self._breakers.clear()

    # NOT a BackgroundService today (no start()). Alias kept as a
    # convenience — close_all() is the real shutdown hook.
    stop = close_all

    async def health_check(self) -> dict[str, bool]:
        """Check health of all registered exporters."""
        results: dict[str, bool] = {}
        for name, exporter in self._exporters.items():
            try:
                results[name] = await exporter.health_check()
            except Exception:
                results[name] = False
        return results
