"""Pyroscope continuous profiling for Nexus.

Issue #763: Continuous profiling via Grafana Pyroscope.

This module provides a standalone Pyroscope integration that sends CPU
profiles to a Pyroscope server for flame-graph analysis, diff flame graphs
across deployments, and trace-to-profile correlation (click a slow Tempo
span to see its flame graph).

Environment Variables:
    PYROSCOPE_ENABLED: Enable/disable profiling (default: "false")
    PYROSCOPE_SERVER_ADDRESS: Pyroscope server URL (default: "http://pyroscope:4040")
    PYROSCOPE_APPLICATION_NAME: Application name tag (default: "nexus.api")
    PYROSCOPE_SAMPLE_RATE: CPU sampling rate in Hz (default: "100")
    PYROSCOPE_AUTH_TOKEN: Optional auth token for Pyroscope Cloud

Usage:
    from nexus.server.profiling import setup_profiling, shutdown_profiling

    # Initialize once at startup (in lifespan)
    setup_profiling()

    # Shutdown at teardown
    shutdown_profiling()
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Global state
_initialized = False


def _parse_sample_rate_hz(env_var: str, default: int = 100) -> int:
    """Parse CPU sampling rate in Hz from an environment variable.

    Args:
        env_var: Environment variable name.
        default: Fallback value if the env var is missing or invalid.

    Returns:
        An integer in [1, 1000].
    """
    raw = os.environ.get(env_var, str(default))
    try:
        rate = int(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid sample rate %r for %s, using default %d Hz", raw, env_var, default)
        return default
    return max(1, min(1000, rate))


def is_profiling_enabled() -> bool:
    """Check if profiling is enabled via PYROSCOPE_ENABLED environment variable."""
    return os.environ.get("PYROSCOPE_ENABLED", "false").lower() in ("true", "1", "yes")


def setup_profiling(
    application_name: str | None = None,
    server_address: str | None = None,
    sample_rate: int | None = None,
    auth_token: str | None = None,
    tags: dict[str, str] | None = None,
) -> bool:
    """Initialize Pyroscope continuous profiling.

    Lazy-imports pyroscope inside function body for graceful fallback
    when the pyroscope-io package is not installed.

    Also registers PyroscopeSpanProcessor on the existing OTel TracerProvider
    if OpenTelemetry is active, enabling trace-to-profile correlation.

    Note:
        Must be called exactly once from the main thread during
        application startup (e.g., in the FastAPI lifespan handler).

    Args:
        application_name: Override PYROSCOPE_APPLICATION_NAME env var.
        server_address: Override PYROSCOPE_SERVER_ADDRESS env var.
        sample_rate: Override PYROSCOPE_SAMPLE_RATE env var (Hz).
        auth_token: Override PYROSCOPE_AUTH_TOKEN env var.
        tags: Additional static tags to attach to profiles.

    Returns:
        True if profiling was initialized, False if disabled or error.
    """
    global _initialized

    if _initialized:
        logger.debug("Pyroscope already initialized, skipping")
        return False

    if not is_profiling_enabled():
        logger.info("Pyroscope disabled (set PYROSCOPE_ENABLED=true to enable)")
        return False

    try:
        import pyroscope

        _app_name = (
            application_name
            if application_name is not None
            else os.environ.get("PYROSCOPE_APPLICATION_NAME", "nexus.api")
        )
        _server_address = (
            server_address
            if server_address is not None
            else os.environ.get("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
        )
        _sample_rate = (
            sample_rate
            if sample_rate is not None
            else _parse_sample_rate_hz("PYROSCOPE_SAMPLE_RATE")
        )
        _auth_token = (
            auth_token if auth_token is not None else os.environ.get("PYROSCOPE_AUTH_TOKEN", "")
        )

        # Build static tags
        _tags = {
            "env": os.environ.get("NEXUS_ENV", "development"),
            "service": "nexus",
        }

        # Add version if available
        try:
            from nexus.server._version import get_nexus_version

            _tags["version"] = get_nexus_version()
        except ImportError:
            pass

        if tags:
            _tags.update(tags)

        pyroscope.configure(
            application_name=_app_name,
            server_address=_server_address,
            sample_rate=_sample_rate,
            auth_token=_auth_token,
            tags=_tags,
            oncpu=True,
            gil_only=True,
            detect_subprocesses=False,  # avoid profiling uvicorn worker forks
        )

        # Register PyroscopeSpanProcessor for trace-to-profile correlation
        _register_otel_processor()

        _initialized = True
        logger.info(
            "Pyroscope initialized: app=%s, server=%s, sample_rate=%d",
            _app_name,
            _server_address,
            _sample_rate,
        )
        return True

    except ImportError:
        logger.info("pyroscope-io not installed (pip install 'nexus-ai-fs[profiling]' to enable)")
        return False
    except Exception as e:
        logger.error("Failed to initialize Pyroscope: %s", e)
        return False


def _register_otel_processor() -> None:
    """Register PyroscopeSpanProcessor on the active OTel TracerProvider.

    This enables trace-to-profile correlation: each OTel span gets a
    profile annotation so Grafana can link Tempo spans to Pyroscope
    flame graphs.

    Silently skips if OTel or pyroscope-otel is not available.
    """
    try:
        from opentelemetry import trace
        from pyroscope.otel import PyroscopeSpanProcessor

        provider = trace.get_tracer_provider()
        if hasattr(provider, "add_span_processor"):
            provider.add_span_processor(PyroscopeSpanProcessor())
            logger.debug("Registered PyroscopeSpanProcessor on TracerProvider")
    except ImportError:
        logger.debug("pyroscope-otel not available, skipping trace-to-profile correlation")
    except Exception as e:
        logger.warning("Failed to register PyroscopeSpanProcessor: %s", e)


def shutdown_profiling() -> None:
    """Shutdown Pyroscope profiling. Idempotent.

    Call this during application shutdown to flush pending profile data.
    """
    global _initialized

    if not _initialized:
        return

    try:
        import pyroscope

        if hasattr(pyroscope, "shutdown"):
            pyroscope.shutdown()
        logger.info("Pyroscope shutdown complete")
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Error during Pyroscope shutdown: %s", e)
    finally:
        _initialized = False
