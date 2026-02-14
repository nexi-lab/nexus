"""Structlog processor for routing errors to Sentry.

Issue #759: Sentry for Error Tracking and Performance.

Provides ``create_sentry_processor()`` which returns a structlog processor
that forwards error-level log events to Sentry via ``structlog-sentry``.

Architecture:
- Checks ``should_alert`` field (set by ``error_classification_processor``):
  if ``False``, the event is NOT sent to Sentry.
- Returns a no-op processor when ``structlog-sentry`` is not installed or
  Sentry is not enabled — zero overhead.
- Module-level caching of availability (same pattern as ``otel_trace_processor``).

Pipeline order::

    ...error_classification_processor → sentry_processor → StackInfoRenderer...

The Sentry processor needs ``exc_info`` before ``format_exc_info`` consumes it.
"""

from __future__ import annotations

from typing import Any

# Cache availability at module level (same pattern as _HAS_OTEL in logging_processors.py).
try:
    from structlog_sentry import SentryProcessor as _SentryProcessor

    _HAS_STRUCTLOG_SENTRY = True
except ImportError:
    _SentryProcessor = None  # type: ignore[assignment, misc]
    _HAS_STRUCTLOG_SENTRY = False


def _noop_processor(_logger: Any, _method_name: Any, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Identity processor — passes through unchanged."""
    return event_dict


def _sentry_filtering_processor(
    logger: Any, method_name: Any, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Processor that checks ``should_alert`` before delegating to SentryProcessor.

    If ``should_alert`` is explicitly ``False`` (i.e., the error is expected),
    skip Sentry entirely. Otherwise, delegate to the real SentryProcessor.
    """
    # Skip expected errors — should_alert=False means "don't page on-call"
    if event_dict.get("should_alert") is False:
        return event_dict

    # Delegate to the real SentryProcessor
    result: dict[str, Any] = _real_processor(logger, method_name, event_dict)
    return result


# Module-level instance — created once, reused for all log events.
_real_processor: Any = None


def create_sentry_processor() -> Any:
    """Create a structlog processor that routes errors to Sentry.

    Returns:
        A structlog processor function. Returns ``_noop_processor`` when
        ``structlog-sentry`` is not installed or Sentry is not enabled.
    """
    global _real_processor

    if not _HAS_STRUCTLOG_SENTRY:
        return _noop_processor

    # Check if Sentry is actually enabled (DSN set)
    try:
        from nexus.server.sentry import is_sentry_enabled

        if not is_sentry_enabled():
            return _noop_processor
    except ImportError:
        return _noop_processor

    # Create the real SentryProcessor instance (cached at module level)
    if _real_processor is None:
        import logging

        _real_processor = _SentryProcessor(event_level=logging.ERROR)

    return _sentry_filtering_processor
