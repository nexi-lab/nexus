"""Custom Prometheus collector bridging WriteBuffer to /metrics (Issue #1370).

Reads write-path counters from a WriteBuffer-backed write observer and
exposes them as Prometheus ``GaugeMetricFamily`` values.  All metrics use
gauges because the counters are plain integers that reset on process restart.

Usage:
    Wired automatically in ``fastapi_server.create_app()``::

        from prometheus_client import REGISTRY
        from nexus.server.wb_metrics_collector import WriteBufferCollector

        REGISTRY.register(WriteBufferCollector(write_observer))
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prometheus_client.core import GaugeMetricFamily

if TYPE_CHECKING:
    from collections.abc import Iterable


@runtime_checkable
class MetricsProvider(Protocol):
    """Duck-typed interface for objects exposing a ``metrics`` property."""

    @property
    def metrics(self) -> dict[str, Any]: ...


class WriteBufferCollector:
    """Prometheus custom collector for WriteBuffer metrics.

    Takes the write observer (BufferedRecordStoreSyncer or any object with
    a ``metrics`` property returning the WriteBuffer metrics dict) as a
    constructor arg -- storage layer stays Prometheus-free.
    """

    def __init__(self, write_observer: MetricsProvider) -> None:
        self._write_observer = write_observer

    def describe(self) -> Iterable[GaugeMetricFamily]:
        """Return empty -- dynamic collector convention."""
        return []

    def collect(self) -> Iterable[GaugeMetricFamily]:
        """Yield gauge metric families from the write observer's counters."""
        metrics = self._write_observer.metrics

        # Per-event-type enqueued breakdown
        enqueued = GaugeMetricFamily(
            "nexus_write_buffer_events_enqueued_total",
            "Total events enqueued into WriteBuffer by event type",
            labels=["event_type"],
        )
        for event_type, count in metrics.get("enqueued_by_type", {}).items():
            enqueued.add_metric([event_type], count)
        yield enqueued

        yield GaugeMetricFamily(
            "nexus_write_buffer_events_flushed_total",
            "Total events successfully flushed from WriteBuffer",
            value=metrics["total_flushed"],
        )
        yield GaugeMetricFamily(
            "nexus_write_buffer_events_failed_total",
            "Total events dropped after exhausting retries",
            value=metrics["total_failed"],
        )
        yield GaugeMetricFamily(
            "nexus_write_buffer_retries_total",
            "Total flush retry attempts",
            value=metrics["total_retries"],
        )
        yield GaugeMetricFamily(
            "nexus_write_buffer_pending_events",
            "Number of events waiting to be flushed",
            value=metrics["pending"],
        )
        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_duration_seconds_sum",
            "Cumulative flush duration in seconds",
            value=metrics["flush_duration_sum"],
        )
        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_duration_seconds_count",
            "Total number of flush operations",
            value=metrics["flush_count"],
        )
        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_batch_size_sum",
            "Cumulative number of events across all flush batches",
            value=metrics["flush_batch_size_sum"],
        )
        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_batch_size_count",
            "Total number of flush operations (for batch size average)",
            value=metrics["flush_count"],
        )
