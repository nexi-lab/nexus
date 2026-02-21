"""Prometheus metrics for event stream export and replay (Issue #1138/#1139).

Centralizes metric definitions for exporters, DLQ, replay, and SSE.
"""

from prometheus_client import Counter, Gauge, Histogram

# ---- Export metrics ----------------------------------------------------------

events_published_total = Counter(
    "nexus_events_published_total",
    "Total events published to external brokers",
    ["broker", "event_type", "topic"],
)

publish_failures_total = Counter(
    "nexus_publish_failures_total",
    "Total export publish failures",
    ["broker", "event_type", "error_class"],
)

publish_duration_seconds = Histogram(
    "nexus_publish_duration_seconds",
    "Duration of publish operations to external brokers",
    ["broker"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
)

# ---- DLQ metrics -------------------------------------------------------------

dlq_depth = Gauge(
    "nexus_dlq_depth",
    "Number of unresolved dead letter queue entries",
    ["exporter_name", "failure_type"],
)

# ---- Replay metrics ----------------------------------------------------------

replay_query_duration_seconds = Histogram(
    "nexus_replay_query_duration_seconds",
    "Duration of event replay queries",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# ---- SSE metrics -------------------------------------------------------------

sse_active_connections = Gauge(
    "nexus_sse_active_connections",
    "Number of active SSE connections",
    ["zone_id"],
)
