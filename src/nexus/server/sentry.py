"""Sentry error tracking and performance monitoring for Nexus.

Issue #759: Sentry for Error Tracking and Performance.

Provides:
- ``setup_sentry()``: Initialize Sentry SDK with FastAPI integration.
- ``shutdown_sentry()``: Flush pending events and reset state.
- ``is_sentry_enabled()``: Check if Sentry DSN is configured.

Architecture:
- **No duplication**: ``LoggingIntegration`` is disabled. The structlog
  ``SentryProcessor`` (in ``sentry_processor.py``) is the ONLY log→Sentry path.
  FastAPI auto-capture is the ONLY unhandled-exception→Sentry path.
- **Opt-in**: No ``SENTRY_DSN`` = zero overhead. No ``sentry_sdk`` imports at
  module level.
- **Correlation**: ``before_send`` reads ``correlation_id_var`` ContextVar and
  sets it as a Sentry tag for cross-referencing with structured logs.

Environment Variables:
    SENTRY_DSN: Sentry Data Source Name (required to enable)
    SENTRY_ENVIRONMENT: Deployment environment (default: "development")
    SENTRY_TRACES_SAMPLE_RATE: Transaction sample rate 0.0-1.0 (default: 0.1)
    SENTRY_PROFILES_SAMPLE_RATE: Profile sample rate 0.0-1.0 (default: 0.0)
    SENTRY_DEBUG: Enable Sentry debug mode (default: false)
    SENTRY_SEND_PII: Send personally identifiable information (default: false)

Usage::

    from nexus.server.sentry import setup_sentry, shutdown_sentry

    # In FastAPI lifespan startup
    setup_sentry()

    # In FastAPI lifespan shutdown
    shutdown_sentry()
"""

from __future__ import annotations

import logging
import os
from typing import Any

from nexus.core.config import SentryConfig

logger = logging.getLogger(__name__)

# Global state — mirrors telemetry.py pattern
_initialized = False


def is_sentry_enabled() -> bool:
    """Check if Sentry is enabled via ``SENTRY_DSN`` environment variable."""
    return bool(os.environ.get("SENTRY_DSN", "").strip())


def _build_config_from_env() -> SentryConfig:
    """Build SentryConfig from environment variables."""
    return SentryConfig(
        dsn=os.environ.get("SENTRY_DSN", "").strip(),
        environment=os.environ.get("SENTRY_ENVIRONMENT", "development"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.0")),
        send_default_pii=os.environ.get("SENTRY_SEND_PII", "false").lower() in (
            "true",
            "1",
            "yes",
        ),
        debug=os.environ.get("SENTRY_DEBUG", "false").lower() in ("true", "1", "yes"),
    )


def _get_release() -> str:
    """Return release identifier as ``nexus@{version}``."""
    try:
        from importlib.metadata import version

        return f"nexus@{version('nexus-ai-fs')}"
    except Exception:
        return "nexus@unknown"


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Sentry ``before_send`` hook.

    - Attaches ``correlation_id`` tag from ContextVar.
    - Drops events for expected errors (``is_expected=True``).
    """
    # Attach correlation_id tag
    try:
        from nexus.server.middleware.correlation import correlation_id_var

        cid = correlation_id_var.get()
        if cid:
            event.setdefault("tags", {})["correlation_id"] = cid
    except Exception:
        pass

    # Filter expected errors
    exc_info = hint.get("exc_info")
    if exc_info is not None:
        _, exc_value, _ = exc_info
        if exc_value is not None and getattr(exc_value, "is_expected", False):
            return None

    return event


def _traces_sampler(sampling_context: dict[str, Any]) -> float:
    """Custom traces sampler — skip health checks, use configured rate for the rest."""
    try:
        # ASGI scope is available in the sampling context for transaction events
        asgi_scope = sampling_context.get("asgi_scope") or {}
        path = asgi_scope.get("path", "")
        if path.startswith("/health"):
            return 0.0
    except Exception:
        pass

    # Fall back to configured rate
    return float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))


def setup_sentry(config: SentryConfig | None = None) -> bool:
    """Initialize Sentry SDK.

    Args:
        config: Optional SentryConfig. If None, reads from environment variables.

    Returns:
        True if Sentry was initialized, False if disabled or already initialized.
    """
    global _initialized

    if _initialized:
        logger.debug("Sentry already initialized, skipping")
        return False

    if config is None:
        config = _build_config_from_env()

    if not config.dsn:
        logger.info("Sentry disabled (set SENTRY_DSN to enable)")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=config.dsn,
            environment=config.environment,
            release=_get_release(),
            traces_sample_rate=config.traces_sample_rate,
            traces_sampler=_traces_sampler,
            profiles_sample_rate=config.profiles_sample_rate,
            send_default_pii=config.send_default_pii,
            max_breadcrumbs=config.max_breadcrumbs,
            attach_stacktrace=config.attach_stacktrace,
            enable_tracing=config.enable_tracing,
            debug=config.debug,
            before_send=_before_send,  # type: ignore[arg-type]
            integrations=[
                # Disable LoggingIntegration to prevent duplication.
                # structlog-sentry SentryProcessor is the ONLY log→Sentry path.
                LoggingIntegration(event_level=None, level=None),
                FastApiIntegration(),
                StarletteIntegration(),
            ],
        )

        _initialized = True
        logger.info(
            "Sentry initialized: environment=%s, traces_sample_rate=%s",
            config.environment,
            config.traces_sample_rate,
        )
        return True

    except ImportError as e:
        logger.warning("sentry-sdk not installed: %s", e)
        return False
    except Exception as e:
        logger.error("Failed to initialize Sentry: %s", e)
        return False


def shutdown_sentry() -> None:
    """Flush pending events and reset Sentry state.

    Call during application shutdown to ensure all events are sent.
    """
    global _initialized

    if not _initialized:
        return

    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=2.0)
        # Reset the SDK by closing the current client
        client = sentry_sdk.get_client()
        if client.is_active():
            client.close()
        logger.info("Sentry shutdown complete")
    except Exception as e:
        logger.warning("Error during Sentry shutdown: %s", e)
    finally:
        _initialized = False
