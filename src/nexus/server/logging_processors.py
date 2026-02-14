"""Custom structlog processors for Nexus.

Issue #1002: Structured JSON logging with request correlation.

Processors:
- ``otel_trace_processor``: Injects OTel trace_id/span_id into log events.
- ``error_classification_processor``: Classifies errors as expected/unexpected.
- ``add_service_name``: Adds ``service`` to every log event (configurable via
  ``NEXUS_SERVICE_NAME`` env var, defaults to ``"nexus"``).
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from typing import Any

# Cache OTel availability at module level (Issue #1002 / Issue 13).
# Python does NOT cache failed imports in sys.modules, so a per-call
# try/except ImportError would retry the full import machinery on every
# log entry when OTel is not installed.
_otel_trace: Any = None
_HAS_OTEL = False
try:
    from opentelemetry import trace

    _otel_trace = trace
    _HAS_OTEL = True
except ImportError:
    pass

# Configurable service name (Issue #1002 / Issue 8).
# Matches OTel convention (OTEL_SERVICE_NAME).
_SERVICE_NAME = os.environ.get("NEXUS_SERVICE_NAME", "nexus")


def otel_trace_processor(
    _logger: Any, _method_name: Any, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Inject OTel trace_id and span_id into the log event dict.

    When an OTel span is active and recording, adds:
    - ``trace_id``: 32-character lowercase hex string (128-bit)
    - ``span_id``: 16-character lowercase hex string (64-bit)

    When OTel is not installed or no span is active, this is a no-op.
    """
    if not _HAS_OTEL:
        return event_dict

    try:
        span = _otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return event_dict

        ctx = span.get_span_context()
        if ctx is not None and ctx.trace_id != 0:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")

    except Exception:
        # Never let tracing break logging
        pass

    return event_dict


def error_classification_processor(
    _logger: Any, _method_name: Any, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Classify errors as expected/unexpected based on ``is_expected`` attribute.

    When ``exc_info`` is present and contains an exception:
    - Checks ``error.is_expected`` attribute (set by Nexus exception classes)
    - Adds ``error_expected: bool`` field
    - Adds ``should_alert: bool`` field (True for unexpected errors only)

    Handles all ``exc_info`` formats:
    - ``True`` (stdlib convention): resolved via ``sys.exc_info()``
    - 3-tuple ``(type, value, traceback)``
    - ``BaseException`` instance (structlog convention)

    Non-error log events pass through unchanged.
    """
    exc_info = event_dict.get("exc_info")

    # Only process if we have actual exception info
    if not exc_info or exc_info is False:
        return event_dict

    # Resolve exc_info=True to the current exception (stdlib convention)
    if exc_info is True:
        exc_info = sys.exc_info()
        # If no exception is active, nothing to classify
        if exc_info[1] is None:
            return event_dict

    if isinstance(exc_info, tuple) and len(exc_info) >= 2:
        exc = exc_info[1]
    elif isinstance(exc_info, BaseException):
        exc = exc_info
    else:
        return event_dict

    if exc is None:
        return event_dict

    is_expected = getattr(exc, "is_expected", False)
    event_dict["error_expected"] = is_expected
    event_dict["should_alert"] = not is_expected

    return event_dict


def add_service_name(
    _logger: Any, _method_name: Any, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Add service name to every log event.

    Reads from ``NEXUS_SERVICE_NAME`` env var at module load time,
    defaulting to ``"nexus"``. Does not overwrite if ``service`` is
    already set (e.g., by a child service).
    """
    if "service" not in event_dict:
        event_dict["service"] = _SERVICE_NAME
    return event_dict
