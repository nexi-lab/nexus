"""Shared telemetry utilities — zero-dependency on nexus internals.

Bricks and services can import ``is_telemetry_enabled`` and ``get_tracer``
from here instead of ``nexus.server.telemetry``, keeping the server layer
out of brick dependency graphs.

The heavy-weight setup (``setup_telemetry``, ``instrument_fastapi_app``,
etc.) remains in ``nexus.server.telemetry``.
"""

import os
from typing import Any


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled via environment variable."""
    return os.environ.get("OTEL_ENABLED", "false").lower() in ("true", "1", "yes")


def get_tracer(name: str | None = None) -> Any:
    """Get an OTel tracer for creating custom spans.

    Returns ``None`` when telemetry is disabled or ``opentelemetry``
    is not installed.  Safe to call from any layer (core, bricks,
    services) without pulling in the server telemetry module.

    Args:
        name: Tracer name (typically ``__name__`` of the calling module).
    """
    if not is_telemetry_enabled():
        return None
    try:
        from opentelemetry import trace

        return trace.get_tracer(name or __name__)
    except Exception:
        return None
