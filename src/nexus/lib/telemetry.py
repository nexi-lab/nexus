"""Tier-neutral OpenTelemetry tracing utilities.

Provides tracer resolution and span helpers that any layer (services,
backends, bricks) can use without importing from ``nexus.server``.

The server layer calls :func:`init_telemetry_state` during startup to
activate tracing; before that, all helpers are safe no-ops.

Issue #913: Extracted from ``nexus.server.telemetry`` to fix
services->server and backends->server import boundary violations.
"""

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

# Global state — set by server.telemetry.setup_telemetry() at startup.
_initialized = False
_tracer: "Tracer | None" = None


def init_telemetry_state(initialized: bool, tracer: "Tracer | None" = None) -> None:
    """Set telemetry global state (called by server layer at startup)."""
    global _initialized, _tracer  # noqa: PLW0603
    _initialized = initialized
    _tracer = tracer


def reset_telemetry_state() -> None:
    """Clear telemetry global state (called by server layer at shutdown)."""
    global _initialized, _tracer  # noqa: PLW0603
    _initialized = False
    _tracer = None


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled via environment variable."""
    return os.environ.get("OTEL_ENABLED", "false").lower() in ("true", "1", "yes")


def get_tracer(name: str | None = None) -> "Tracer | None":
    """Get a tracer for creating custom spans.

    Returns None if telemetry is not initialized (safe no-op).
    """
    if not _initialized:
        return None

    try:
        from opentelemetry import trace

        return trace.get_tracer(name or __name__)
    except Exception:
        return None


def add_span_attribute(key: str, value: str | int | float | bool) -> None:
    """Add an attribute to the current span (no-op if telemetry disabled)."""
    if not _initialized:
        return

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span:
            span.set_attribute(key, value)
    except Exception:
        pass  # Telemetry must never disrupt application flow


def record_exception(exception: Exception) -> None:
    """Record an exception in the current span (no-op if telemetry disabled)."""
    if not _initialized:
        return

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span:
            span.record_exception(exception)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exception)))
    except Exception:
        pass  # Telemetry must never disrupt application flow
