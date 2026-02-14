"""Sentry error tracking and performance monitoring for Nexus.

Issue #759: Sentry for error tracking and performance.

This module provides a standalone Sentry integration that runs independently
of the OpenTelemetry pipeline (dual-stack approach). It captures unhandled
and unexpected exceptions, attaches request correlation IDs, and optionally
enables performance tracing.

Environment Variables:
    SENTRY_DSN: Sentry DSN (required to enable — no DSN = Sentry disabled)
    SENTRY_ENVIRONMENT: Deployment environment (default: NEXUS_ENV or "development")
    SENTRY_TRACES_SAMPLE_RATE: Performance trace sample rate 0.0-1.0 (default: "0.0")
    SENTRY_PROFILES_SAMPLE_RATE: Profiling sample rate 0.0-1.0 (default: "0.0")
    SENTRY_SEND_DEFAULT_PII: Include PII in events (default: "false")

Usage:
    from nexus.server.sentry import setup_sentry, shutdown_sentry

    # Initialize once at startup (in lifespan)
    setup_sentry()

    # Shutdown at teardown
    shutdown_sentry()

Note:
    ``setup_sentry()`` must be called exactly once from the main thread during
    application startup (e.g., in the FastAPI lifespan handler).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

logger = logging.getLogger(__name__)

# Global state
_initialized = False

# Resolved traces sample rate — set by setup_sentry(), used by _sentry_traces_sampler().
# Stored at module level so the sampler closure respects parameter overrides.
_resolved_traces_rate: float = 0.0

# Status codes that trigger Sentry error reporting (5xx only).
_FAILED_STATUS_CODES: frozenset[int] = frozenset(range(500, 600))

# Endpoints to exclude from performance traces.
_SKIP_TRACE_PREFIXES = ("/health", "/metrics", "/favicon")


def is_sentry_enabled() -> bool:
    """Check if Sentry is enabled via SENTRY_DSN environment variable.

    Sentry is enabled if and only if a non-empty SENTRY_DSN is set.
    """
    return bool(os.environ.get("SENTRY_DSN", "").strip())


def _get_version() -> str:
    """Get Nexus version for Sentry release tag."""
    try:
        from importlib.metadata import version

        return version("nexus-ai-fs")
    except Exception:
        return "unknown"


def _parse_sample_rate(env_var: str, default: float = 0.0) -> float:
    """Parse a sample rate from an environment variable, clamping to [0.0, 1.0].

    Args:
        env_var: Environment variable name.
        default: Fallback value if the env var is missing or invalid.

    Returns:
        A float in [0.0, 1.0].
    """
    raw = os.environ.get(env_var, str(default))
    try:
        rate = float(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid sample rate %r for %s, using default %s", raw, env_var, default)
        return default
    return max(0.0, min(1.0, rate))


def sentry_before_send(event: Event, hint: Hint) -> Event | None:
    """Filter events before sending to Sentry.

    - Drops events for expected errors (is_expected=True) — user input
      validation, not-found, permission denied, etc.
    - Attaches correlation_id as a tag for cross-referencing with structured logs.

    Args:
        event: The Sentry event dict.
        hint: Contains ``exc_info`` tuple when an exception triggered the event.

    Returns:
        The event dict (possibly modified) to send, or None to drop.
    """
    # Check if the exception is an expected user error
    exc_info = hint.get("exc_info")
    if exc_info is not None:
        exc = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) >= 2 else None
        if exc is not None and getattr(exc, "is_expected", False):
            return None

    # Attach correlation_id as a Sentry tag for log cross-referencing
    try:
        from nexus.server.middleware.correlation import correlation_id_var

        correlation_id = correlation_id_var.get()
        if correlation_id:
            tags = event.setdefault("tags", {})
            tags["correlation_id"] = correlation_id
    except ImportError:
        logger.debug("CorrelationMiddleware not available; skipping correlation_id tag")

    return event


def _sentry_traces_sampler(sampling_context: dict[str, Any]) -> float:
    """Custom traces sampler that filters out noisy endpoints.

    Drops performance traces for health checks, metrics, and OPTIONS requests
    when performance monitoring is enabled.

    Uses the resolved rate from ``setup_sentry()`` (stored in ``_resolved_traces_rate``)
    so that parameter overrides are respected.

    Args:
        sampling_context: Context dict with transaction info.

    Returns:
        Sample rate (0.0 to drop, resolved rate otherwise).
    """
    # If traces are disabled, short-circuit
    if _resolved_traces_rate <= 0.0:
        return 0.0

    # Filter noisy endpoints
    transaction_context = sampling_context.get("transaction_context", {})
    name = transaction_context.get("name", "")

    # Drop health checks, metrics, and favicon
    if any(name.startswith(prefix) for prefix in _SKIP_TRACE_PREFIXES):
        return 0.0

    # Drop OPTIONS requests (pre-flight CORS)
    # Check ASGI scope for method (FastAPI uses ASGI, not WSGI)
    asgi_scope = sampling_context.get("asgi_scope", {})
    if asgi_scope.get("method") == "OPTIONS":
        return 0.0

    # Also check WSGI environ for compatibility
    if sampling_context.get("wsgi_environ", {}).get("REQUEST_METHOD") == "OPTIONS":
        return 0.0

    return _resolved_traces_rate


def setup_sentry(
    dsn: str | None = None,
    environment: str | None = None,
    traces_sample_rate: float | None = None,
    profiles_sample_rate: float | None = None,
    send_default_pii: bool | None = None,
) -> bool:
    """Initialize Sentry error tracking and performance monitoring.

    Must be called exactly once from the main thread during startup.

    Args:
        dsn: Override SENTRY_DSN env var.
        environment: Override SENTRY_ENVIRONMENT env var.
        traces_sample_rate: Override SENTRY_TRACES_SAMPLE_RATE env var.
        profiles_sample_rate: Override SENTRY_PROFILES_SAMPLE_RATE env var.
        send_default_pii: Override SENTRY_SEND_DEFAULT_PII env var.

    Returns:
        True if Sentry was initialized, False if disabled or error.
    """
    global _initialized, _resolved_traces_rate

    if _initialized:
        logger.debug("Sentry already initialized, skipping")
        return False

    _dsn = dsn or os.environ.get("SENTRY_DSN", "").strip()
    if not _dsn:
        logger.info("Sentry disabled (set SENTRY_DSN to enable)")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        _environment = environment or os.environ.get(
            "SENTRY_ENVIRONMENT",
            os.environ.get("NEXUS_ENV", "development"),
        )

        _resolved_traces_rate = (
            traces_sample_rate
            if traces_sample_rate is not None
            else _parse_sample_rate("SENTRY_TRACES_SAMPLE_RATE")
        )

        _profiles_sample_rate = (
            profiles_sample_rate
            if profiles_sample_rate is not None
            else _parse_sample_rate("SENTRY_PROFILES_SAMPLE_RATE")
        )

        _send_default_pii = (
            send_default_pii
            if send_default_pii is not None
            else (
                os.environ.get("SENTRY_SEND_DEFAULT_PII", "false").lower() in ("true", "1", "yes")
            )
        )

        version = _get_version()

        sentry_sdk.init(
            dsn=_dsn,
            environment=_environment,
            release=f"nexus@{version}",
            before_send=sentry_before_send,
            traces_sampler=_sentry_traces_sampler,
            profiles_sample_rate=_profiles_sample_rate,
            send_default_pii=_send_default_pii,
            integrations=[
                FastApiIntegration(
                    transaction_style="url",
                    failed_request_status_codes=_FAILED_STATUS_CODES,
                ),
                StarletteIntegration(
                    transaction_style="url",
                    failed_request_status_codes=_FAILED_STATUS_CODES,
                ),
                LoggingIntegration(
                    level=logging.WARNING,
                    event_level=None,
                ),
            ],
        )

        _initialized = True
        logger.info(
            "Sentry initialized: environment=%s, release=nexus@%s, "
            "traces_sample_rate=%s, profiles_sample_rate=%s, pii=%s",
            _environment,
            version,
            _resolved_traces_rate,
            _profiles_sample_rate,
            _send_default_pii,
        )
        return True

    except ImportError:
        logger.info("sentry-sdk not installed (pip install 'nexus-ai-fs[sentry]' to enable)")
        return False
    except Exception as e:
        logger.error("Failed to initialize Sentry: %s", e)
        return False


def shutdown_sentry() -> None:
    """Flush pending events and shutdown Sentry.

    Call this during application shutdown to ensure all events are sent.
    """
    global _initialized, _resolved_traces_rate

    if not _initialized:
        return

    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=2.0)
        logger.info("Sentry shutdown complete")
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Error during Sentry shutdown: %s", e)
    finally:
        _initialized = False
        _resolved_traces_rate = 0.0
