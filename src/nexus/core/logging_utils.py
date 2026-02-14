"""Logging utilities for Nexus error classification.

.. deprecated::
    This module's functions still work but are superseded by the
    ``error_classification_processor`` in ``nexus.server.logging_processors``
    (Issue #1002). With structlog configured, error classification is automatic
    via the processor pipeline — no need to call ``log_error()`` manually.

This module provides utilities for logging errors based on their classification
(expected vs unexpected), enabling cleaner logs and better observability.

Usage (legacy):
    from nexus.core.logging_utils import log_error

    try:
        result = operation()
    except NexusError as e:
        log_error(logger, e, operation="read_file")
        raise

Usage (preferred — with structlog configured):
    logger.error("operation failed", exc_info=e, operation="read_file")
    # error_classification_processor auto-adds error_expected + should_alert
"""

from __future__ import annotations

import logging


def log_error(
    logger: logging.Logger,
    error: Exception,
    *,
    operation: str | None = None,
    context: dict | None = None,
    include_traceback: bool | None = None,
) -> None:
    """Log an error at the appropriate level based on its classification.

    Expected errors (user errors like validation, not found, permission denied)
    are logged at INFO level without stack traces.

    Unexpected errors (system errors like backend failures, bugs) are logged
    at ERROR level with full stack traces.

    Args:
        logger: The logger instance to use
        error: The exception to log
        operation: Optional operation name for context (e.g., "read_file")
        context: Optional additional context to include in log message
        include_traceback: Override automatic traceback decision. If None,
                          includes traceback only for unexpected errors.

    Examples:
        >>> log_error(logger, NexusFileNotFoundError("/path/to/file"))
        # Logs at INFO: "Expected error: File not found: /path/to/file"

        >>> log_error(logger, BackendError("Connection failed"))
        # Logs at ERROR with traceback: "System error: Connection failed"

        >>> log_error(logger, error, operation="sync_files", context={"backend": "gcs"})
        # Includes operation and context in log message
    """
    is_expected = getattr(error, "is_expected", False)

    # Build context string
    parts = []
    if operation:
        parts.append(f"operation={operation}")
    if context:
        parts.extend(f"{k}={v}" for k, v in context.items())
    context_str = f" [{', '.join(parts)}]" if parts else ""

    # Determine traceback behavior
    if include_traceback is None:
        include_traceback = not is_expected

    if is_expected:
        logger.info(f"Expected error{context_str}: {error}")
    else:
        logger.error(f"System error{context_str}: {error}", exc_info=include_traceback)


def log_exception(
    logger: logging.Logger,
    error: Exception,
    message: str,
    *,
    operation: str | None = None,
    context: dict | None = None,
) -> None:
    """Log an exception with a custom message, respecting error classification.

    This is a convenience wrapper around log_error for cases where you want
    to provide a custom message rather than using the exception's str().

    Args:
        logger: The logger instance to use
        error: The exception that occurred
        message: Custom message to log
        operation: Optional operation name for context
        context: Optional additional context

    Examples:
        >>> log_exception(logger, e, "Failed to process file", operation="parse")
        # Logs at appropriate level based on error.is_expected
    """
    is_expected = getattr(error, "is_expected", False)

    # Build context string
    parts = []
    if operation:
        parts.append(f"operation={operation}")
    if context:
        parts.extend(f"{k}={v}" for k, v in context.items())
    context_str = f" [{', '.join(parts)}]" if parts else ""

    if is_expected:
        logger.info(f"{message}{context_str}: {error}")
    else:
        logger.error(f"{message}{context_str}: {error}", exc_info=True)


def should_alert(error: Exception) -> bool:
    """Determine if an error should trigger an alert.

    Only unexpected errors (system failures, bugs) should trigger alerts.
    Expected errors (user input issues) should not.

    Args:
        error: The exception to check

    Returns:
        True if the error should trigger an alert, False otherwise

    Examples:
        >>> should_alert(NexusFileNotFoundError("/missing"))
        False  # Expected error, don't alert

        >>> should_alert(BackendError("Database connection failed"))
        True  # Unexpected error, should alert
    """
    return not getattr(error, "is_expected", False)


def get_log_level_for_error(error: Exception) -> int:
    """Get the appropriate log level for an error.

    Args:
        error: The exception to get log level for

    Returns:
        logging.INFO for expected errors, logging.ERROR for unexpected
    """
    is_expected = getattr(error, "is_expected", False)
    return logging.INFO if is_expected else logging.ERROR
