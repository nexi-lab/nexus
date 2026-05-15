"""Prometheus metrics for the MCP HTTP hub frontend (#3873)."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

MCP_REQUESTS = Counter(
    "nexus_mcp_requests_total",
    "Total MCP HTTP requests observed by the hub frontend",
    labelnames=["rpc_method", "tool_name", "status"],
)

MCP_REQUEST_LATENCY = Histogram(
    "nexus_mcp_request_latency_seconds",
    "MCP HTTP request latency in seconds observed by the hub frontend",
    labelnames=["rpc_method", "tool_name", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

MCP_ACTIVE_CLIENTS = Gauge(
    "nexus_mcp_active_clients",
    "Distinct MCP hub clients seen in the active window",
)

MCP_ERRORS = Counter(
    "nexus_mcp_errors_total",
    "Total MCP HTTP error responses observed by the hub frontend",
    labelnames=["rpc_method", "tool_name", "status"],
)

_ACTIVE_CLIENT_TTL_SECONDS = 60.0
_active_clients: dict[str, float] = {}
_active_clients_lock = threading.Lock()


def metrics_enabled() -> bool:
    """Return whether the MCP /metrics route should be registered."""
    return os.environ.get("NEXUS_MCP_METRICS_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _label(value: Any) -> str:
    if value is None:
        return "unknown"
    text = str(value).strip()
    return text if text else "unknown"


def _status_group(status_code: Any) -> str:
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = 500
    return f"{status // 100}xx"


def _client_id(record: dict[str, Any]) -> str:
    return _label(record.get("subject_id") or record.get("token_hash") or "anonymous")


def _refresh_active_clients(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    cutoff = current - _ACTIVE_CLIENT_TTL_SECONDS
    with _active_clients_lock:
        stale = [client_id for client_id, seen_at in _active_clients.items() if seen_at < cutoff]
        for client_id in stale:
            _active_clients.pop(client_id, None)
        MCP_ACTIVE_CLIENTS.set(len(_active_clients))


def record_request_metrics(record: dict[str, Any]) -> None:
    """Record one MCP audit record into the Prometheus registry."""
    rpc_method = _label(record.get("rpc_method"))
    tool_name = _label(record.get("tool_name"))
    status = _status_group(record.get("status_code"))
    labels = {
        "rpc_method": rpc_method,
        "tool_name": tool_name,
        "status": status,
    }

    try:
        latency_seconds = max(0.0, float(record.get("latency_ms", 0)) / 1000.0)
    except (TypeError, ValueError):
        latency_seconds = 0.0

    MCP_REQUESTS.labels(**labels).inc()
    MCP_REQUEST_LATENCY.labels(**labels).observe(latency_seconds)
    if status.startswith(("4", "5")):
        MCP_ERRORS.labels(**labels).inc()

    with _active_clients_lock:
        _active_clients[_client_id(record)] = time.monotonic()
    _refresh_active_clients()


def render_metrics() -> bytes:
    """Render the current global Prometheus registry."""
    _refresh_active_clients()
    return generate_latest()


def install_metrics_route(mcp_server: Any) -> bool:
    """Register GET /metrics on a FastMCP server when enabled by env."""
    if not metrics_enabled():
        return False

    @mcp_server.custom_route("/metrics", methods=["GET"])
    async def metrics_endpoint(_request: Any) -> Response:
        return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)

    return True


def _reset_for_tests() -> None:
    """Reset in-process MCP metric state for focused unit tests."""
    for metric in (MCP_REQUESTS, MCP_REQUEST_LATENCY, MCP_ERRORS):
        children = metric._metrics
        children.clear()
    with _active_clients_lock:
        _active_clients.clear()
    MCP_ACTIVE_CLIENTS.set(0)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "install_metrics_route",
    "metrics_enabled",
    "record_request_metrics",
    "render_metrics",
]
