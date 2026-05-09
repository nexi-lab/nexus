"""Prometheus metrics catalog for issue #3791 foundation slice.

Registered at import time on the global ``prometheus_client.REGISTRY``.
``record_metrics(...)`` is invoked from ``QueueEmitter.emit`` so the Prom
state stays accurate even if the SQLite sink drops batches.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from nexus.contracts.protocols.activity import register_approvals_pending_gauge
from nexus.services.activity.events import EventKind, Result

# Issue's named catalog ───────────────────────────────────────────────────────

SEARCH_REQUESTS = Counter(
    "nexus_search_requests_total",
    "Total Nexus search requests",
    ["zone", "status"],
)

SEARCH_LATENCY = Histogram(
    "nexus_search_latency_seconds",
    "Nexus search request latency in seconds",
    ["zone"],
)

MCP_TOOL_CALLS = Counter(
    "nexus_mcp_tool_calls_total",
    "Total MCP tool calls dispatched",
    ["tool", "status"],
)

POLICY_BLOCKS = Counter(
    "nexus_policy_blocks_total",
    "Total ReBAC/zone-access denials",
    ["kind"],
)

APPROVALS_PENDING = Gauge(
    "nexus_approvals_pending",
    "Number of approval requests currently in PENDING state",
)

# Wire the contracts-side reseed entrypoint to this gauge so brick callers
# (which only import nexus.contracts.protocols.activity) can update it
# without crossing the contracts→services boundary.
register_approvals_pending_gauge(APPROVALS_PENDING.set)

# Internal subsystem health ───────────────────────────────────────────────────

ACTIVITY_DROPS = Counter(
    "nexus_activity_drops_total",
    "Activity events dropped due to queue overflow",
)

ACTIVITY_SINK_ERRORS = Counter(
    "nexus_activity_sink_errors_total",
    "Activity sink batch-write errors",
    ["sink"],
)

ACTIVITY_RETENTION_PRUNED = Counter(
    "nexus_activity_retention_pruned_total",
    "Activity events pruned by retention task",
)

AGENT_LOG_LINES_DROPPED = Counter(
    "nexus_activity_agent_log_lines_dropped_total",
    "Lines not written to agent_log mount, by reason",
    ["reason"],  # ring_evict | recursion | no_agent
)

AGENT_LOG_BYTES = Gauge(
    "nexus_activity_agent_log_bytes",
    "Current bytes held in the agent_log MemoryBackend",
    ["agent_id"],
)


def record_metrics(
    *,
    kind: EventKind,
    result: Result,
    actor_token_hash: str | None,
    subject_zone: str | None,
    subject_extra: dict[str, Any] | None,
    latency_ms: int | None,
) -> None:
    """Update the Prom catalog for one emitted event.

    Token attribution lives in the SQLite activity event payload, not in
    Prometheus labels — token rotation produces unbounded series otherwise.

    APPROVALS_PENDING is NOT updated here. The gauge is authoritative —
    ``ApprovalService`` reseeds it from the durable ``list_pending`` count
    at startup and after every transition (create / decide / local-timeout
    expire / sweeper expire / cross-worker NOTIFY / reconcile) via
    ``reseed_approvals_pending``. Event-delta accounting cannot stay
    consistent across cross-worker decisions where the decrement and the
    increment happen in different processes.

    MCP_TOOL_CALL contract: producers MUST populate
    ``subject_extra={"tool": ...}`` for the MCP_TOOL_CALLS counter
    label to reflect the actual tool name (otherwise it falls back
    to "unknown").
    """
    _ = actor_token_hash  # retained for SQLite payload; not a Prom label
    zone = subject_zone or "unknown"

    if kind is EventKind.SEARCH:
        SEARCH_REQUESTS.labels(zone=zone, status=result.value).inc()
        if latency_ms is not None:
            SEARCH_LATENCY.labels(zone=zone).observe(latency_ms / 1000.0)
    elif kind is EventKind.MCP_TOOL_CALL:
        tool = (subject_extra or {}).get("tool", "unknown")
        MCP_TOOL_CALLS.labels(tool=tool, status=result.value).inc()
    elif kind in (EventKind.ZONE_ACCESS, EventKind.POLICY_BLOCK):
        if result is Result.BLOCKED:
            POLICY_BLOCKS.labels(kind=kind.value).inc()
    # APPROVAL: gauge is reseeded from durable state by the producer.
    # FETCH currently has no metric in the catalog.
