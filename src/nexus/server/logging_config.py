"""Centralized structured logging configuration.

Issue #1002: Structured JSON logging with request correlation.

Provides a single ``configure_logging()`` function that sets up structlog
with a processor pipeline and routes ALL loggers (stdlib, uvicorn, sqlalchemy,
etc.) through structlog's ``ProcessorFormatter``.

- **Dev mode**: Pretty, colored console output via ``ConsoleRenderer``
- **Prod mode**: Machine-readable JSON via ``JSONRenderer`` + ``orjson``

**Relationship to ObservabilitySubsystem (#1301):**
This module handles *log formatting and routing* — it is server-level
infrastructure (analogous to uvicorn config). The ``ObservabilitySubsystem``
in ``nexus.core.subsystems`` manages *metric/trace lifecycle* (OTel provider
setup, span creation, SQLAlchemy listeners). The ``otel_trace_processor`` in
``logging_processors.py`` bridges the two by reading (not managing) the
current OTel span context.

Usage::

    from nexus.server.logging_config import configure_logging

    # Called once in FastAPI lifespan — the single canonical call site
    configure_logging(env="prod")              # JSON output
    configure_logging(env="dev")               # Pretty console
    configure_logging(env="prod", log_level="DEBUG")  # Override level
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys
from typing import Any

import orjson
import structlog

from nexus.server.logging_processors import (
    add_service_name,
    error_classification_processor,
    otel_trace_processor,
)


def _orjson_serializer(data: dict[str, Any], **_kw: Any) -> str:
    """Serialize log event dict to JSON string using orjson.

    Uses ``default=str`` as fallback for non-serializable types (e.g., sets,
    custom objects) to prevent log entry loss.
    """
    return orjson.dumps(
        data,
        option=orjson.OPT_NON_STR_KEYS | orjson.OPT_UTC_Z,
        default=str,
    ).decode("utf-8")


def configure_logging(
    env: str = "dev",
    log_level: str | None = None,
    _handler_override: logging.Handler | None = None,
) -> None:
    """Configure structured logging for the entire application.

    Sets up structlog with a shared processor pipeline and routes all stdlib
    loggers through structlog's ``ProcessorFormatter``.

    Args:
        env: Environment mode. ``"dev"`` for pretty console, ``"prod"`` for JSON.
        log_level: Override log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            If ``None``, reads ``LOG_LEVEL`` env var, defaulting to ``"INFO"``.
        _handler_override: Internal testing parameter. When set, this handler
            is used instead of the default ``StreamHandler(sys.stderr)``.
    """
    # Validate env
    if env not in ("dev", "prod"):
        raise ValueError(f"Invalid env: {env!r}. Must be 'dev' or 'prod'.")

    # Resolve and validate log level
    level_name = (log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, None)
    if level is None:
        raise ValueError(
            f"Invalid log level: {level_name!r}. "
            "Valid levels: DEBUG, INFO, WARNING, ERROR, CRITICAL"
        )

    # Shared processors run for BOTH structlog-native and stdlib-bridged logs.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_service_name,
        otel_trace_processor,
        error_classification_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # Renderer: JSON for prod, pretty console for dev
    if env == "prod":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer(
            serializer=_orjson_serializer,
        )
    else:
        renderer = structlog.dev.ConsoleRenderer()

    # Configure structlog itself (for structlog.get_logger() calls)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Build the ProcessorFormatter that stdlib loggers will use
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    # Determine the handler
    if _handler_override is not None:
        handler = _handler_override
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(formatter)

    # Configure the root logger to use our handler
    root_logger = logging.getLogger()
    # Clear existing handlers to prevent duplicates on re-configuration
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Route well-known third-party loggers through our formatter
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "sqlalchemy.engine",
        "httpx",
        "aiohttp",
    ):
        third_party = logging.getLogger(logger_name)
        third_party.handlers.clear()
        third_party.propagate = True

    # Suppress uvicorn's default access log — CorrelationMiddleware provides
    # a superior structured access log with correlation_id, status_code, and
    # duration_ms as separate queryable fields.
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers.clear()
    uvicorn_access.setLevel(logging.WARNING)
