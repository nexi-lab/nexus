"""Prometheus metrics middleware and /metrics endpoint for Nexus.

Issue #761: Grafana + Loki + Tempo unified observability.

Exposes HTTP request metrics (duration, count, in-progress) via a
``/metrics`` endpoint scraped by Prometheus.  A lightweight ASGI
middleware records per-request measurements with low-cardinality labels
(method, status group, route template).

Usage:
    Wired automatically in ``fastapi_server.create_app()``::

        from nexus.server.metrics import PrometheusMiddleware, metrics_endpoint
        app.add_middleware(PrometheusMiddleware)
        app.add_route("/metrics", metrics_endpoint, methods=["GET"])
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "status", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    labelnames=["method", "status", "endpoint"],
)

REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently in progress",
    labelnames=["method"],
)

NEXUS_INFO = Info(
    "nexus",
    "Nexus server information",
)

# Paths excluded from metric recording (noisy, internal).
_SKIP_PATHS: frozenset[str] = frozenset({"/health", "/metrics", "/favicon.ico"})


def _status_group(status_code: int) -> str:
    """Return a low-cardinality status group like ``2xx``."""
    return f"{status_code // 100}xx"


def _resolve_route_template(scope: Scope) -> str:
    """Resolve the route template from the ASGI scope.

    Falls back to the raw path when no matching route is found.
    """
    fallback: str = str(scope.get("path", "unknown"))

    app = scope.get("app")
    if app is None:
        return fallback

    # Walk the router to find the matched route template
    for route in getattr(app, "routes", []):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            path: str | None = getattr(route, "path", None)
            return path if path is not None else fallback

    return fallback


# ---------------------------------------------------------------------------
# ASGI Middleware
# ---------------------------------------------------------------------------


class PrometheusMiddleware:
    """ASGI middleware that records Prometheus request metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "GET")
        REQUESTS_IN_PROGRESS.labels(method=method).inc()
        start = time.perf_counter()

        status_code = 500  # default in case of unhandled error

        # Capture the status code from the response start message
        original_send = send

        async def _send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status", 500)
            await original_send(message)

        try:
            await self.app(scope, receive, _send_wrapper)  # type: ignore[arg-type]
        finally:
            duration = time.perf_counter() - start
            endpoint = _resolve_route_template(scope)
            status = _status_group(status_code)

            REQUEST_DURATION.labels(method=method, status=status, endpoint=endpoint).observe(
                duration
            )
            REQUEST_COUNT.labels(method=method, status=status, endpoint=endpoint).inc()
            REQUESTS_IN_PROGRESS.labels(method=method).dec()


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------

_METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


async def metrics_endpoint(request: Request) -> Response:  # noqa: ARG001
    """Serve Prometheus metrics in the exposition format."""
    return Response(
        content=generate_latest(),
        media_type=_METRICS_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Setup helper (called from lifespan)
# ---------------------------------------------------------------------------


def setup_prometheus() -> None:
    """Populate the nexus info metric with the current version."""
    from nexus.server._version import get_nexus_version

    version = get_nexus_version()
    NEXUS_INFO.info({"version": version})
    logger.info("Prometheus metrics initialized (version=%s)", version)
