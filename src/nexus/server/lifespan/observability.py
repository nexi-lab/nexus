"""Observability startup/shutdown: logging, Sentry, OpenTelemetry, Pyroscope, Prometheus.

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def startup_observability(app: FastAPI) -> None:
    """Initialize all observability subsystems (sync, no background tasks)."""
    _startup_logging()
    _startup_sentry()
    _startup_telemetry()
    _startup_profiling()
    _startup_prometheus()
    _startup_thread_pool(app)


def shutdown_observability() -> None:
    """Shutdown observability subsystems in reverse order."""
    # Shutdown Sentry (Issue #759) -- flush pending events
    try:
        from nexus.server.sentry import shutdown_sentry

        shutdown_sentry()
    except ImportError:
        pass

    # Shutdown OpenTelemetry (Issue #764)
    try:
        from nexus.server.telemetry import shutdown_telemetry

        shutdown_telemetry()
    except ImportError:
        pass

    # Shutdown Pyroscope continuous profiling (Issue #763)
    try:
        from nexus.server.profiling import shutdown_profiling

        shutdown_profiling()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _startup_logging() -> None:
    """Configure structured logging (Issue #1002)."""
    try:
        from nexus.server.logging_config import configure_logging

        env = os.environ.get("NEXUS_ENV", "dev")
        configure_logging(env=env)
    except ImportError:
        pass  # structlog not installed -- fall back to stdlib

    logger.info("Starting FastAPI Nexus server...")


def _startup_sentry() -> None:
    """Initialize Sentry error tracking (Issue #759)."""
    try:
        from nexus.server.sentry import setup_sentry

        setup_sentry()
    except ImportError:
        logger.debug("Sentry not available")


def _startup_telemetry() -> None:
    """Initialize OpenTelemetry (Issue #764)."""
    try:
        from nexus.server.telemetry import setup_telemetry

        setup_telemetry()
    except ImportError:
        logger.debug("OpenTelemetry not available")


def _startup_profiling() -> None:
    """Initialize Pyroscope continuous profiling (Issue #763)."""
    try:
        from nexus.server.profiling import setup_profiling

        setup_profiling()
    except ImportError:
        logger.debug("Pyroscope not available")


def _startup_prometheus() -> None:
    """Initialize Prometheus metrics (Issue #761)."""
    try:
        from nexus.server.metrics import setup_prometheus

        setup_prometheus()
    except ImportError:
        logger.debug("prometheus_client not available")


def _startup_thread_pool(app: FastAPI) -> None:
    """Configure thread pool size (Issue #932)."""
    from anyio import to_thread

    limiter = to_thread.current_default_thread_limiter()
    limiter.total_tokens = app.state.thread_pool_size
    logger.info(f"Thread pool size set to {limiter.total_tokens}")
