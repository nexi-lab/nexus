"""Prometheus metrics catalog for issue #3791 foundation slice.

Registered at import time on the global ``prometheus_client.REGISTRY``.
``record_metrics(...)`` is invoked from ``QueueEmitter.emit`` so the Prom
state stays accurate even if the SQLite sink drops batches.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from nexus.services.activity.events import EventKind, Result

# Issue's named catalog ───────────────────────────────────────────────────────

SEARCH_REQUESTS = Counter(
    "nexus_search_requests_total",
    "Total Nexus search requests",
    ["zone", "token_hash", "status"],
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

    APPROVALS_PENDING contract: producers MUST emit exactly one
    PENDING_APPROVAL when an approval is created and exactly one
    non-PENDING result (OK | BLOCKED) when it resolves. Across a
    process restart the gauge may go negative for in-flight approvals
    that resolve after restart — Task 15 (ApprovalService wiring)
    should reseed APPROVALS_PENDING from a DB count at startup if
    accuracy is required.

    MCP_TOOL_CALL contract: producers MUST populate
    ``subject_extra={"tool": ...}`` for the MCP_TOOL_CALLS counter
    label to reflect the actual tool name (otherwise it falls back
    to "unknown").
    """
    zone = subject_zone or "unknown"
    token = actor_token_hash or "anonymous"

    if kind is EventKind.SEARCH:
        SEARCH_REQUESTS.labels(zone=zone, token_hash=token, status=result.value).inc()
        if latency_ms is not None:
            SEARCH_LATENCY.labels(zone=zone).observe(latency_ms / 1000.0)
    elif kind is EventKind.MCP_TOOL_CALL:
        tool = (subject_extra or {}).get("tool", "unknown")
        MCP_TOOL_CALLS.labels(tool=tool, status=result.value).inc()
    elif kind in (EventKind.ZONE_ACCESS, EventKind.POLICY_BLOCK):
        if result is Result.BLOCKED:
            POLICY_BLOCKS.labels(kind=kind.value).inc()
    elif kind is EventKind.APPROVAL:
        if result is Result.PENDING_APPROVAL:
            APPROVALS_PENDING.inc()
        else:
            APPROVALS_PENDING.dec()
    # FETCH currently has no metric in the catalog.
