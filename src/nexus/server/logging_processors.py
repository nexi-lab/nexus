"""Custom structlog processors for Nexus.

Issue #1002: Structured JSON logging with request correlation.
Issue #86: Secret redaction in log output.

Processors:
- ``secret_redaction_processor``: Masks secrets (API keys, tokens, passwords,
  connection strings) in log event text. Aligned with Templar TS patterns.
- ``otel_trace_processor``: Injects OTel trace_id/span_id into log events.
- ``error_classification_processor``: Classifies errors as expected/unexpected.
- ``add_service_name``: Adds ``service`` to every log event (configurable via
  ``NEXUS_SERVICE_NAME`` env var, defaults to ``"nexus"``).

Secret Redaction (Issue #86):
    Uses a structlog processor to redact secrets from the ``event`` field
    (and optionally other string fields) before rendering. Patterns are
    aligned with Templar TypeScript @templar/middleware/audit/redaction.ts.

    A ``RedactingFormatter`` is also provided for stdlib-only contexts
    (e.g., FUSE daemon process) where structlog is not available.

    Performance: Heuristic pre-check skips regex for ~95% of log lines
    that contain no trigger substrings. Target: <1ms overhead per line.
"""

import logging
import os
import re
import sys
from collections.abc import MutableMapping
from typing import Any, Literal

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

# ============================================================================
# SECRET REDACTION (Issue #86)
# ============================================================================
#
# Cross-reference: These patterns are aligned with the Templar TypeScript
# audit middleware at @templar/middleware/src/audit/redaction.ts.
# When adding/modifying patterns here, update the TS counterpart too.
#
# Pattern names must match between Python and TypeScript for consistency.
# ============================================================================

_REDACTED = "[REDACTED]"


class _RedactionPattern:
    """A named, pre-compiled redaction pattern."""

    __slots__ = ("name", "regex", "replacement")

    def __init__(
        self,
        name: str,
        pattern: str,
        flags: int = re.IGNORECASE,
        replacement: str = _REDACTED,
    ) -> None:
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
    r"(?:FERNET_KEY|fernet_key|encryption_key)\s*[=:]\s*[A-Za-z0-9_\-]{43}=",
    flags=0,  # Case-sensitive; require keyword context to avoid false positives
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
    _PEM_PRIVATE_KEY,  # Most distinctive (multi-line)
    _NEXUS_API_KEY,  # Before generic sk- (more specific)
    _BEARER_TOKEN,  # "Bearer " prefix is distinctive
    _JWT_TOKEN,  # "eyJ" prefix is distinctive
    _AWS_ACCESS_KEY,  # AKIA/ASIA prefix
    _AWS_SECRET_KEY,  # Named key assignment
    _SK_API_KEY,  # sk- prefix (generic)
    _POSTGRES_URL,  # Protocol-specific URLs
    _MYSQL_URL,
    _MONGODB_URL,
    _REDIS_URL,
    _DJANGO_SECRET_KEY,  # Named key assignment
    _GENERIC_API_KEY_ASSIGNMENT,  # Named key assignment (broad)
    _GENERIC_PASSWORD,  # Named value assignment (broadest)
    _FERNET_KEY,  # Base64 string (broadest, last)
)

# Trigger substrings for heuristic pre-check (all lowercase).
# If none of these appear in a lowercased log line, skip all regex matching.
# This optimizes the common case (~95%+ of log lines have no secrets).
# Using lowercase + one .lower() call is cheaper and more maintainable
# than listing case variants for every keyword.
_TRIGGER_SUBSTRINGS_LOWER: tuple[str, ...] = (
    "bearer",
    "sk-",
    "://",
    "key",
    "password",
    "passwd",
    "pwd",
    "begin",
    "akia",
    "asia",
    "eyj",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "fernet",
    "encryption_key",
)


def redact_text(
    text: str,
    patterns: tuple[_RedactionPattern, ...] | None = None,
) -> str:
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
    # One .lower() call is cheaper than scanning for case variants of every keyword.
    if patterns is BUILT_IN_SECRET_PATTERNS:
        text_lower = text.lower()
        if not any(trigger in text_lower for trigger in _TRIGGER_SUBSTRINGS_LOWER):
            return text

    result = text
    for rp in patterns:
        result = rp.regex.sub(rp.replacement, result)
    return result


# Fields that never contain user-supplied secrets — skip for performance.
_SKIP_FIELDS: frozenset[str] = frozenset(
    {
        "log_level",
        "timestamp",
        "logger",
        "logger_name",
        "service",
        "trace_id",
        "span_id",
        "error_expected",
        "should_alert",
        "level",
    }
)


def make_secret_redaction_processor(
    *,
    enabled: bool = True,
) -> Any:
    """Factory that creates a structlog processor with captured enabled state.

    Uses a closure instead of a mutable module-level flag, following
    immutability principles. The ``enabled`` state is captured at creation
    time and cannot be changed afterward.

    Args:
        enabled: Whether to enable secret redaction. Defaults to True
            (secure by default).

    Returns:
        A structlog processor function with the standard
        ``(logger, method_name, event_dict) -> event_dict`` signature.
    """

    def _processor(
        _logger: Any, _method_name: Any, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Redact secrets from all string fields in the log event dict.

        Scans the ``event`` (message) string and all other string-valued fields
        for known secret patterns (API keys, tokens, passwords, connection
        strings) and replaces them with ``[REDACTED]``.

        Skips structlog-internal fields (log_level, timestamp, etc.) for
        performance — these never contain user-supplied secrets.

        Uses a heuristic pre-check to skip regex for ~95% of log lines that
        contain no trigger substrings, keeping overhead negligible.

        Patterns are aligned with Templar TypeScript
        @templar/middleware/src/audit/redaction.ts.
        """
        if not enabled:
            return event_dict

        # structlog convention: processors mutate event_dict in place
        for key, value in event_dict.items():
            if key not in _SKIP_FIELDS and isinstance(value, str):
                event_dict[key] = redact_text(value)

        return event_dict

    return _processor


# Default processor instance (enabled=True) for backward compatibility
# and direct use without factory.
secret_redaction_processor = make_secret_redaction_processor(enabled=True)


class RedactingFormatter(logging.Formatter):
    """A logging.Formatter that masks secrets in log output.

    For use in stdlib-only logging contexts (e.g., FUSE daemon process)
    where structlog is not available. For structlog pipelines, use
    ``secret_redaction_processor`` instead.

    Includes a heuristic pre-check that skips regex matching for log lines
    that don't contain any trigger substrings, optimizing the common case.
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
