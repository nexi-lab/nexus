"""Logging utilities for Nexus error classification and secret redaction.

<<<<<<< Updated upstream
.. deprecated::
    This module's functions still work but are superseded by the
    ``error_classification_processor`` in ``nexus.server.logging_processors``
    (Issue #1002). With structlog configured, error classification is automatic
    via the processor pipeline — no need to call ``log_error()`` manually.

This module provides utilities for logging errors based on their classification
(expected vs unexpected), enabling cleaner logs and better observability.

Usage (legacy):
=======
This module provides:
- Error classification logging (log_error, log_exception)
- Secret redaction via RedactingFormatter (Issue #86)

Secret Redaction (Issue #86):
    RedactingFormatter intercepts ALL log output (app code, uvicorn, SQLAlchemy,
    third-party libraries) and masks secrets using regex patterns. Patterns are
    aligned with Templar TypeScript @templar/middleware/audit/redaction.ts.

    Usage:
        from nexus.core.logging_utils import setup_logging

        # In server startup:
        setup_logging(level=logging.INFO)

        # All subsequent log calls are automatically redacted:
        logger.info(f"Connected to {db_url}")
        # Output: "Connected to [REDACTED]"

Error Classification:
>>>>>>> Stashed changes
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
import re
from typing import Any, Literal

# ============================================================================
# SECRET REDACTION PATTERNS (Issue #86)
# ============================================================================
#
# Cross-reference: These patterns are aligned with the Templar TypeScript
# audit middleware at @templar/middleware/src/audit/redaction.ts.
# When adding/modifying patterns here, update the TS counterpart too.
#
# Pattern names must match between Python and TypeScript for consistency.
# See tests/unit/core/test_log_redaction.py for pattern parity validation.
# ============================================================================

_REDACTED = "[REDACTED]"


class _RedactionPattern:
    """A named, pre-compiled redaction pattern."""

    __slots__ = ("name", "regex", "replacement")

    def __init__(self, name: str, pattern: str, flags: int = re.IGNORECASE, replacement: str = _REDACTED) -> None:
        self.name = name
        self.regex = re.compile(pattern, flags)
        self.replacement = replacement


# --- Templar TS parity patterns (10 patterns) ---

_BEARER_TOKEN = _RedactionPattern(
    "bearer_token",
    r"Bearer\s+[\w\-._~+/]{10,}=*",
)

_SK_API_KEY = _RedactionPattern(
    "sk_api_key",
    r"sk-[a-zA-Z0-9]{20,}",
)

_POSTGRES_URL = _RedactionPattern(
    "postgres_url",
    r"postgres(?:ql)?://[^\s\"']+",
)

_MYSQL_URL = _RedactionPattern(
    "mysql_url",
    r"mysql://[^\s\"']+",
)

_MONGODB_URL = _RedactionPattern(
    "mongodb_url",
    r"mongodb(?:\+srv)?://[^\s\"']+",
)

_REDIS_URL = _RedactionPattern(
    "redis_url",
    r"redis://[^\s\"']+",
)

_PEM_PRIVATE_KEY = _RedactionPattern(
    "pem_private_key",
    r"-----BEGIN [A-Z\s]*PRIVATE KEY-----[\s\S]*?-----END [A-Z\s]*PRIVATE KEY-----",
)

_AWS_ACCESS_KEY = _RedactionPattern(
    "aws_access_key",
    r"(?:AKIA|ASIA)[A-Z0-9]{16}",
    flags=0,  # Case-sensitive for AWS keys
)

_AWS_SECRET_KEY = _RedactionPattern(
    "aws_secret_key",
    r"(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*[A-Za-z0-9/+=]{40}",
)

_GENERIC_PASSWORD = _RedactionPattern(
    "generic_password",
    r"(?:password|passwd|pwd)\s*[=:]\s*[\"']?[^\s\"']{8,}[\"']?",
)

# --- Python-specific patterns (additional to Templar TS) ---

_NEXUS_API_KEY = _RedactionPattern(
    "nexus_api_key",
    r"sk-[a-zA-Z0-9]+_[a-zA-Z0-9]+_[a-f0-9\-]+_[a-f0-9]+",
)

_DJANGO_SECRET_KEY = _RedactionPattern(
    "django_secret_key",
    r"(?:SECRET_KEY|DJANGO_SECRET_KEY)\s*[=:]\s*[\"']?[^\s\"']{20,}[\"']?",
)

_FERNET_KEY = _RedactionPattern(
    "fernet_key",
    r"[A-Za-z0-9_\-]{43}=",
    flags=0,  # Case-sensitive; exactly 44 chars (base64 of 32 bytes)
)

_JWT_TOKEN = _RedactionPattern(
    "jwt_token",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    flags=0,  # Case-sensitive for base64url
)

_GENERIC_API_KEY_ASSIGNMENT = _RedactionPattern(
    "generic_api_key_assignment",
    r"(?:api_key|apikey|api-key|access_token|auth_token)\s*[=:]\s*[\"']?[^\s\"']{10,}[\"']?",
)


# The default set of patterns, ordered from most specific to least specific
# to minimize false-positive overlap.
BUILT_IN_SECRET_PATTERNS: tuple[_RedactionPattern, ...] = (
    _PEM_PRIVATE_KEY,       # Most distinctive (multi-line)
    _NEXUS_API_KEY,         # Before generic sk- (more specific)
    _BEARER_TOKEN,          # "Bearer " prefix is distinctive
    _JWT_TOKEN,             # "eyJ" prefix is distinctive
    _AWS_ACCESS_KEY,        # AKIA/ASIA prefix
    _AWS_SECRET_KEY,        # Named key assignment
    _SK_API_KEY,            # sk- prefix (generic)
    _POSTGRES_URL,          # Protocol-specific URLs
    _MYSQL_URL,
    _MONGODB_URL,
    _REDIS_URL,
    _DJANGO_SECRET_KEY,     # Named key assignment
    _GENERIC_API_KEY_ASSIGNMENT,  # Named key assignment (broad)
    _GENERIC_PASSWORD,      # Named value assignment (broadest)
    _FERNET_KEY,            # Base64 string (broadest, last)
)

# Trigger substrings for heuristic pre-check.
# If none of these appear in a log line, skip all regex matching.
# This optimizes the common case (~95%+ of log lines have no secrets).
_TRIGGER_SUBSTRINGS: tuple[str, ...] = (
    "Bearer",
    "bearer",
    "sk-",
    "://",
    "KEY",
    "key",
    "Key",
    "password",
    "Password",
    "PASSWORD",
    "passwd",
    "pwd",
    "BEGIN",
    "AKIA",
    "ASIA",
    "eyJ",
    "token",
    "Token",
    "TOKEN",
    "api_key",
    "apikey",
    "api-key",
)


def redact_text(text: str, patterns: tuple[_RedactionPattern, ...] | None = None) -> str:
    """Apply redaction patterns to a text string.

    Uses a heuristic pre-check: if the text contains none of the trigger
    substrings, skips all regex matching for performance. The pre-check
    only applies to built-in patterns; custom patterns always run regex
    since their triggers are unknown.

    Args:
        text: The text to redact.
        patterns: Redaction patterns to apply. Defaults to BUILT_IN_SECRET_PATTERNS.

    Returns:
        Text with secrets replaced by [REDACTED].
    """
    if not text:
        return text

    if patterns is None:
        patterns = BUILT_IN_SECRET_PATTERNS

    # Heuristic pre-check: only for built-in patterns (we know their triggers).
    # Custom patterns skip this check since we don't know their trigger strings.
    # Use identity check (`is`) to detect built-in vs custom.
    if patterns is BUILT_IN_SECRET_PATTERNS and not any(trigger in text for trigger in _TRIGGER_SUBSTRINGS):
        return text

    result = text
    for rp in patterns:
        result = rp.regex.sub(rp.replacement, result)
    return result


class RedactingFormatter(logging.Formatter):
    """A logging.Formatter that masks secrets in log output.

    Intercepts the final formatted log string and applies regex-based
    redaction patterns before output. This is the last gate before log
    lines reach stdout/stderr/file, ensuring ALL log calls (including
    third-party libraries) are redacted.

    Includes a heuristic pre-check that skips regex matching for log lines
    that don't contain any trigger substrings, optimizing the common case.

    Cross-reference: Patterns are aligned with Templar TypeScript
    @templar/middleware/src/audit/redaction.ts BUILT_IN_SECRET_PATTERNS.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: Literal["%", "{", "$"] = "%",
        *,
        patterns: tuple[_RedactionPattern, ...] | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(fmt, datefmt, style)
        self._patterns = patterns if patterns is not None else BUILT_IN_SECRET_PATTERNS
        self._enabled = enabled

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record and redact secrets from the output."""
        formatted = super().format(record)
        if not self._enabled:
            return formatted
        return redact_text(formatted, self._patterns)


# ============================================================================
# LOGGING SETUP
# ============================================================================

_DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging(
    *,
    level: int = logging.INFO,
    fmt: str = _DEFAULT_FORMAT,
    redaction_enabled: bool = True,
    custom_patterns: tuple[_RedactionPattern, ...] | None = None,
    stream: Any = None,
) -> None:
    """Configure logging with RedactingFormatter for the entire process.

    Replaces any existing root handler configuration with explicit handler
    setup. Ensures RedactingFormatter is applied to ALL loggers including
    uvicorn access logs and third-party libraries.

    Args:
        level: Log level (default: logging.INFO).
        fmt: Log format string (default: standard Nexus format).
        redaction_enabled: Whether to enable secret redaction (default: True).
        custom_patterns: Additional redaction patterns beyond built-in defaults.
        stream: Output stream (default: sys.stderr via StreamHandler).
    """
    patterns = BUILT_IN_SECRET_PATTERNS
    if custom_patterns:
        patterns = BUILT_IN_SECRET_PATTERNS + custom_patterns

    formatter = RedactingFormatter(fmt, enabled=redaction_enabled, patterns=patterns)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    # Replace all existing handlers on root logger
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Ensure uvicorn access logger also uses our formatter
    for uvicorn_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.handlers.clear()
        uv_logger.addHandler(handler)
        uv_logger.propagate = False


# ============================================================================
# ERROR CLASSIFICATION (existing functionality)
# ============================================================================


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
