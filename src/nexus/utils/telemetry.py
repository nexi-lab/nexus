"""Shared telemetry utilities — zero-dependency on nexus internals.

Bricks and services can import ``is_telemetry_enabled`` from here
instead of ``nexus.server.telemetry``, keeping the server layer
out of brick dependency graphs.

The heavy-weight setup (``setup_telemetry``, ``instrument_fastapi_app``,
``get_tracer``, etc.) remains in ``nexus.server.telemetry``.
"""

import os


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled via environment variable."""
    return os.environ.get("OTEL_ENABLED", "false").lower() in ("true", "1", "yes")
