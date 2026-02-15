"""Custom Prometheus collector bridging QueryObserver to /metrics (Issue #762).

Reads app-level database counters from a ``QueryObserver`` instance and
exposes them as Prometheus ``GaugeMetricFamily`` values.  All metrics use
gauges because the counters are plain integers that reset on process restart.

Usage:
    Wired automatically in ``fastapi_server.create_app()``::

        from prometheus_client import REGISTRY
        from nexus.server.pg_metrics_collector import QueryObserverCollector

        REGISTRY.register(QueryObserverCollector(observer))
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client.core import GaugeMetricFamily

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nexus.services.subsystems.observability_subsystem import QueryObserver


class QueryObserverCollector:
    """Prometheus custom collector that reads from a QueryObserver.

    Takes the observer as a constructor arg — no import of the
    observability subsystem at collection time.
    """

    def __init__(self, observer: QueryObserver) -> None:
        self._observer = observer

    def describe(self) -> Iterable[GaugeMetricFamily]:
        """Return empty — dynamic collector convention."""
        return []

    def collect(self) -> Iterable[GaugeMetricFamily]:
        """Yield gauge metric families from the observer's counters."""
        obs = self._observer

        yield GaugeMetricFamily(
            "nexus_db_queries_total",
            "Total SQL queries observed by QueryObserver",
            value=obs.total_queries,
        )
        yield GaugeMetricFamily(
            "nexus_db_slow_queries_total",
            "Total slow SQL queries (above threshold)",
            value=obs.slow_queries,
        )
        yield GaugeMetricFamily(
            "nexus_db_observer_errors_total",
            "Total QueryObserver listener errors",
            value=obs.error_count,
        )
        yield GaugeMetricFamily(
            "nexus_db_observer_disabled",
            "Whether QueryObserver is disabled (circuit breaker tripped)",
            value=int(obs.disabled),
        )
        yield GaugeMetricFamily(
            "nexus_db_pool_checkouts_total",
            "Total connection pool checkouts",
            value=obs.pool_checkouts,
        )
        yield GaugeMetricFamily(
            "nexus_db_pool_checkins_total",
            "Total connection pool checkins",
            value=obs.pool_checkins,
        )
        yield GaugeMetricFamily(
            "nexus_db_pool_connects_total",
            "Total new pool connections created",
            value=obs.pool_connects,
        )
        yield GaugeMetricFamily(
            "nexus_db_pool_invalidations_total",
            "Total pool connection invalidations",
            value=obs.pool_invalidations,
        )
