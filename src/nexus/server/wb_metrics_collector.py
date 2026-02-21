"""Custom Prometheus collector bridging WriteBuffer metrics to /metrics (Issue #1370).

Reads app-level WriteBuffer counters and exposes them as Prometheus metric families.
Uses GaugeMetricFamily for counters (they reset on process restart) and for
summary-style metrics (sum/count pairs).

Usage:
    Wired automatically in ``fastapi_server.create_app()``::

        from prometheus_client import REGISTRY
        from nexus.server.wb_metrics_collector import WriteBufferCollector

        REGISTRY.register(WriteBufferCollector(write_observer))
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from prometheus_client.core import GaugeMetricFamily

if TYPE_CHECKING:
    from collections.abc import Iterable


class WriteBufferCollector:
    """Prometheus custom collector that reads from a WriteBuffer's metrics property.

    Takes the write_observer (WriteBuffer instance) as a constructor arg.
    """

    def __init__(self, write_observer: Any) -> None:
        self._wo = write_observer

    def describe(self) -> Iterable[GaugeMetricFamily]:
        """Return empty -- dynamic collector convention."""
        return []

    def collect(self) -> Iterable[GaugeMetricFamily]:
        """Yield metric families from the WriteBuffer's metrics dict."""
        m = self._wo.metrics

        # --- Counters (as gauges, since they reset on restart) ---

        # Enqueued by event type (write/delete/rename)
        enqueued_family = GaugeMetricFamily(
            "nexus_write_buffer_events_enqueued_total",
            "Total events enqueued into the write buffer",
            labels=["event_type"],
        )
        enqueued_by_type: dict[str, int] = m.get("enqueued_by_type", {})
        # Emit all five event types so tests can find them
        for event_type in ("write", "delete", "rename", "mkdir", "rmdir"):
            enqueued_family.add_metric([event_type], float(enqueued_by_type.get(event_type, 0)))
        yield enqueued_family

        yield GaugeMetricFamily(
            "nexus_write_buffer_events_flushed_total",
            "Total events successfully flushed to PostgreSQL",
            value=float(m.get("total_flushed", 0)),
        )

        yield GaugeMetricFamily(
            "nexus_write_buffer_events_failed_total",
            "Total events that failed to flush",
            value=float(m.get("total_failed", 0)),
        )

        yield GaugeMetricFamily(
            "nexus_write_buffer_retries_total",
            "Total flush retry attempts",
            value=float(m.get("total_retries", 0)),
        )

        yield GaugeMetricFamily(
            "nexus_write_buffer_pending_events",
            "Number of events currently pending in the buffer",
            value=float(m.get("pending", 0)),
        )

        # --- Summary-style pairs (sum + count) ---

        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_duration_seconds_sum",
            "Sum of flush durations in seconds",
            value=float(m.get("flush_duration_sum", 0.0)),
        )

        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_duration_seconds_count",
            "Number of flush operations",
            value=float(m.get("flush_count", 0)),
        )

        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_batch_size_sum",
            "Sum of flush batch sizes",
            value=float(m.get("flush_batch_size_sum", 0)),
        )

        yield GaugeMetricFamily(
            "nexus_write_buffer_flush_batch_size_count",
            "Number of flush batches",
            value=float(m.get("flush_count", 0)),
        )
