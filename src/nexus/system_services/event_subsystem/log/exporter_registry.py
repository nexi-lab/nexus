"""ExporterRegistry — manages and dispatches to registered exporters.

Dispatches event batches to all registered exporters in parallel via
asyncio.gather(). Collects per-exporter failures for DLQ routing.

Issue #1138: Event Stream Export.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.system_services.event_subsystem.log.exporter_protocol import (
        EventStreamExporterProtocol,
    )
    from nexus.system_services.event_subsystem.types import FileEvent

logger = logging.getLogger(__name__)


class ExporterRegistry:
    """Registry of event stream exporters with parallel dispatch."""

    def __init__(self) -> None:
        self._exporters: dict[str, EventStreamExporterProtocol] = {}

    def register(self, exporter: "EventStreamExporterProtocol") -> None:
        """Register an exporter. Overwrites if name already exists."""
        self._exporters[exporter.name] = exporter
        logger.info("Registered event exporter: %s", exporter.name)

    def unregister(self, name: str) -> None:
        """Remove an exporter by name."""
        removed = self._exporters.pop(name, None)
        if removed:
            logger.info("Unregistered event exporter: %s", name)

    @property
    def exporter_names(self) -> list[str]:
        return list(self._exporters)

    async def dispatch_batch(self, events: "list[FileEvent]") -> dict[str, list[str]]:
        """Dispatch a batch of events to all registered exporters in parallel.

        Returns:
            Dict mapping exporter_name → list of failed event_ids.
            Empty dict values mean all events succeeded for that exporter.
        """
        if not self._exporters or not events:
            return {}

        async def _dispatch_one(
            name: str, exporter: "EventStreamExporterProtocol"
        ) -> tuple[str, list[str]]:
            try:
                failed_ids = await exporter.publish_batch(events)
                return (name, failed_ids)
            except Exception:
                logger.exception("Exporter %s batch dispatch failed", name)
                return (name, [e.event_id for e in events])

        results = await asyncio.gather(
            *[_dispatch_one(name, exp) for name, exp in self._exporters.items()]
        )
        return {name: failed for name, failed in results if failed}

    async def close_all(self) -> None:
        """Gracefully close all registered exporters."""
        for name, exporter in self._exporters.items():
            try:
                await exporter.close()
                logger.info("Closed exporter: %s", name)
            except Exception:
                logger.exception("Error closing exporter: %s", name)
        self._exporters.clear()

    async def health_check(self) -> dict[str, bool]:
        """Check health of all registered exporters."""
        results: dict[str, bool] = {}
        for name, exporter in self._exporters.items():
            try:
                results[name] = await exporter.health_check()
            except Exception:
                results[name] = False
        return results
