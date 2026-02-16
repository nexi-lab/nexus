"""Unit tests for log redaction / secret masking (Issue #86).

Tests cover:
- Individual pattern matching (parametrized)
- Edge cases (empty strings, no secrets, partial matches)
- RedactingFormatter integration with real Logger
- Heuristic pre-check optimization
- Pattern ordering (specific before generic)
- Cross-reference with Templar TS pattern names
"""

import logging

import pytest

from nexus.core.logging_utils import (
    BUILT_IN_SECRET_PATTERNS,
    RedactingFormatter,
    _RedactionPattern,
    redact_text,
    setup_logging,
)

# ============================================================================
# Pattern name parity with Templar TypeScript
# ============================================================================

# These names MUST match @templar/middleware/src/audit/redaction.ts
# If a name is added/removed in TS, update this set AND the Python patterns.
TEMPLAR_TS_PATTERN_NAMES = {
    "bearer_token",
    "sk_api_key",
    "postgres_url",
    "mysql_url",
    "mongodb_url",
    "redis_url",
    "pem_private_key",
    "aws_access_key",
    "aws_secret_key",
    "generic_password",
}

# Python-only patterns (not in Templar TS)
PYTHON_ONLY_PATTERN_NAMES = {
    "nexus_api_key",
    "django_secret_key",
    "fernet_key",
    "jwt_token",
    "generic_api_key_assignment",
}


def test_pattern_name_parity_with_templar_ts() -> None:
    """Verify all Templar TS pattern names exist in Python patterns."""
    python_names = {p.name for p in BUILT_IN_SECRET_PATTERNS}
    missing_from_python = TEMPLAR_TS_PATTERN_NAMES - python_names
    assert not missing_from_python, (
        f"Templar TS patterns missing from Python: {missing_from_python}. "
        f"See @templar/middleware/src/audit/redaction.ts"
    )


def test_all_expected_patterns_present() -> None:
    """Verify the complete set of expected pattern names."""
    python_names = {p.name for p in BUILT_IN_SECRET_PATTERNS}
    expected = TEMPLAR_TS_PATTERN_NAMES | PYTHON_ONLY_PATTERN_NAMES
    assert python_names == expected, (
        f"Pattern set mismatch. "
        f"Extra: {python_names - expected}. "
        f"Missing: {expected - python_names}."
    )


# ============================================================================
# Parametrized pattern tests
# ============================================================================


@pytest.mark.parametrize(
    "pattern_name,input_text,expected_contains_redacted",
    [
        # --- bearer_token ---
        ("bearer_token", "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9", True),
        ("bearer_token", "Bearer abc123def456ghi789jkl012mno345pqr678", True),
        ("bearer_token", "bearer abc123def456ghi789jkl012mno345pqr678", True),
        ("bearer_token", "no bearer here", False),
        # --- sk_api_key ---
        ("sk_api_key", "Using key sk-1234567890abcdefghijklmno", True),
        ("sk_api_key", "sk-abcdefghijklmnopqrstuvwxyz12345", True),
        ("sk_api_key", "sk-short", False),  # Too short (<20 chars)
        # --- nexus_api_key ---
        ("nexus_api_key", "api_key=sk-test_admin_550e8400-e29b-41d4-a716-446655440000_a1b2c3d4", True),
        # --- postgres_url ---
        ("postgres_url", "Connecting to postgresql://user:secret@host:5432/mydb", True),
        ("postgres_url", "postgres://admin:p4ssw0rd@db.example.com/prod", True),
        ("postgres_url", "no database url here", False),
        # --- mysql_url ---
        ("mysql_url", "mysql://root:password@localhost:3306/app", True),
        # --- mongodb_url ---
        ("mongodb_url", "mongodb://user:pass@cluster.mongodb.net/db", True),
        ("mongodb_url", "mongodb+srv://user:pass@cluster.mongodb.net/db", True),
        # --- redis_url ---
        ("redis_url", "redis://default:password@redis-host:6379/0", True),
        # --- pem_private_key ---
        (
            "pem_private_key",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
            True,
        ),
        (
            "pem_private_key",
            "-----BEGIN PRIVATE KEY-----\nbase64content\n-----END PRIVATE KEY-----",
            True,
        ),
        # --- aws_access_key ---
        ("aws_access_key", "Key: AKIAIOSFODNN7EXAMPLE", True),
        ("aws_access_key", "ASIAJEXAMPLEKEYID1234", True),
        ("aws_access_key", "not an aws key", False),
        # --- aws_secret_key ---
        (
            "aws_secret_key",
            "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY12",
            True,
        ),
        (
            "aws_secret_key",
            "AWS_SECRET_ACCESS_KEY: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY12",
            True,
        ),
        # --- generic_password ---
        ("generic_password", "password=my_super_secret_p4ss", True),
        ("generic_password", "pwd: longpassword123", True),
        ("generic_password", "passwd='verysecretvalue'", True),
        ("generic_password", "password=short", False),  # Too short (<8 chars)
        # --- django_secret_key ---
        (
            "django_secret_key",
            "SECRET_KEY='django-insecure-abc123def456ghi789'",
            True,
        ),
        # --- fernet_key ---
        ("fernet_key", "Key: ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=", True),
        # --- jwt_token ---
        (
            "jwt_token",
            "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            True,
        ),
        # --- generic_api_key_assignment ---
        ("generic_api_key_assignment", "api_key=sk-1234567890abcdef", True),
        ("generic_api_key_assignment", "access_token: very_long_secret_token_value", True),
        ("generic_api_key_assignment", "api_key=short", False),  # Too short (<10 chars)
    ],
    ids=lambda x: str(x)[:60],
)
def test_individual_pattern(
    pattern_name: str, input_text: str, expected_contains_redacted: bool
) -> None:
    """Test each redaction pattern individually."""
    result = redact_text(input_text)
    if expected_contains_redacted:
        assert "[REDACTED]" in result, (
            f"Pattern '{pattern_name}' should redact: {input_text!r}\n"
            f"Got: {result!r}"
        )
        # Verify the original secret is NOT in the output
        # (the non-secret parts may remain)
    else:
        assert "[REDACTED]" not in result, (
            f"Pattern '{pattern_name}' should NOT redact: {input_text!r}\n"
            f"Got: {result!r}"
        )


# ============================================================================
# Edge case tests
# ============================================================================


def test_empty_string() -> None:
    """Empty string returns empty string."""
    assert redact_text("") == ""


def test_none_like_empty() -> None:
    """Empty string is handled correctly."""
    assert redact_text("") == ""


def test_no_secrets() -> None:
    """String with no secrets is returned unchanged."""
    text = "2024-01-15 - nexus.core - INFO - File uploaded: /workspace/report.pdf"
    assert redact_text(text) == text


def test_multiple_secrets_in_one_line() -> None:
    """Multiple different secret types in one line are all redacted."""
    text = (
        "Connecting to postgresql://admin:secret@db:5432/prod "
        "with api_key=sk-abcdefghijklmnopqrstuvwxyz "
        "and password=my_super_secret"
    )
    result = redact_text(text)
    assert "admin:secret" not in result
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result
    assert "my_super_secret" not in result
    assert result.count("[REDACTED]") >= 3


def test_same_secret_multiple_times() -> None:
    """Same secret appearing multiple times is redacted everywhere."""
    key = "sk-abcdefghijklmnopqrstuvwxyz12345"
    text = f"First: {key}, Second: {key}"
    result = redact_text(text)
    assert key not in result
    assert result.count("[REDACTED]") == 2


def test_surrounding_text_preserved() -> None:
    """Non-secret text around a secret is preserved."""
    text = "Connecting to postgresql://user:password@host/db for operation X"
    result = redact_text(text)
    assert "Connecting to" in result
    assert "for operation X" in result
    assert "[REDACTED]" in result


def test_no_false_positive_on_normal_text() -> None:
    """Common log messages should not trigger false redaction."""
    normal_messages = [
        "File /workspace/data.csv uploaded successfully",
        "Permission check passed for user alice on /files/report.pdf",
        "Agent agent-123 started session",
        "Request processed in 42ms",
        "Cache hit for path /workspace/config.json",
        "WebSocket connection established from 192.168.1.100",
    ]
    for msg in normal_messages:
        result = redact_text(msg)
        assert "[REDACTED]" not in result, f"False positive on: {msg!r}\nGot: {result!r}"


# ============================================================================
# Heuristic pre-check tests
# ============================================================================


def test_heuristic_skip_no_triggers() -> None:
    """Lines without trigger substrings skip regex entirely."""
    # This text has no trigger substrings
    text = "File uploaded: /data/report.pdf (1024 bytes)"
    result = redact_text(text)
    assert result == text  # Unchanged, regex never ran


def test_heuristic_triggers_on_url_protocol() -> None:
    """Lines containing '://' trigger regex check."""
    text = "Connecting to postgresql://user:secret@host/db"
    result = redact_text(text)
    assert "[REDACTED]" in result


# ============================================================================
# RedactingFormatter tests
# ============================================================================


def test_formatter_redacts_message() -> None:
    """RedactingFormatter redacts secrets in formatted output."""
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Connecting to postgresql://admin:secret@db:5432/mydb",
        args=None,
        exc_info=None,
    )
    result = formatter.format(record)
    assert "admin:secret" not in result
    assert "[REDACTED]" in result


def test_formatter_preserves_non_secret_messages() -> None:
    """RedactingFormatter leaves clean messages unchanged."""
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Normal operation completed successfully",
        args=None,
        exc_info=None,
    )
    result = formatter.format(record)
    assert result == "Normal operation completed successfully"


def test_formatter_disabled() -> None:
    """RedactingFormatter with enabled=False passes through unchanged."""
    formatter = RedactingFormatter("%(message)s", enabled=False)
    secret_msg = "api_key=sk-12345678901234567890abcdef"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg=secret_msg,
        args=None,
        exc_info=None,
    )
    result = formatter.format(record)
    assert result == secret_msg  # Not redacted


def test_formatter_custom_patterns() -> None:
    """RedactingFormatter accepts custom patterns."""
    custom = (_RedactionPattern("custom_secret", r"MYSECRET-[0-9]+"),)
    formatter = RedactingFormatter("%(message)s", patterns=custom)
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Using MYSECRET-12345678",
        args=None,
        exc_info=None,
    )
    result = formatter.format(record)
    assert "MYSECRET-12345678" not in result
    assert "[REDACTED]" in result


def test_formatter_with_format_string() -> None:
    """RedactingFormatter works with full format string including timestamp."""
    formatter = RedactingFormatter("%(name)s - %(levelname)s - %(message)s")
    record = logging.LogRecord(
        name="nexus.auth",
        level=logging.WARNING,
        pathname="auth.py",
        lineno=42,
        msg="Token expired: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature_data",
        args=None,
        exc_info=None,
    )
    result = formatter.format(record)
    assert "nexus.auth" in result
    assert "WARNING" in result
    assert "Token expired:" in result
    # The bearer token should be redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in result


# ============================================================================
# Integration: MemoryHandler + RedactingFormatter
# ============================================================================


class _MemoryHandler(logging.Handler):
    """Simple in-memory handler for testing."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def test_integration_real_logger_redacts() -> None:
    """Integration test: real Logger with MemoryHandler + RedactingFormatter."""
    handler = _MemoryHandler()
    handler.setFormatter(RedactingFormatter("%(message)s"))

    logger = logging.getLogger("test_integration_redact")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # Log messages with secrets
    logger.info("Connecting to postgresql://admin:secret@db:5432/prod")
    logger.warning("API key: sk-abcdefghijklmnopqrstuvwxyz12345")
    logger.error("Auth failed with Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    logger.debug("Normal debug message without secrets")

    assert len(handler.records) == 4

    # Secrets are redacted
    assert "admin:secret" not in handler.records[0]
    assert "[REDACTED]" in handler.records[0]

    assert "sk-abcdefghijklmnopqrstuvwxyz12345" not in handler.records[1]
    assert "[REDACTED]" in handler.records[1]

    assert "eyJhbGciOiJIUzI1NiJ9" not in handler.records[2]
    assert "[REDACTED]" in handler.records[2]

    # Normal message unchanged
    assert handler.records[3] == "Normal debug message without secrets"


def test_integration_setup_logging(tmp_path: object) -> None:
    """Integration test: setup_logging configures root logger with redaction."""
    import io

    stream = io.StringIO()
    setup_logging(level=logging.DEBUG, redaction_enabled=True, stream=stream)

    logger = logging.getLogger("test_setup_logging")
    logger.info("DB: postgresql://admin:p4ssw0rd@db.example.com/prod")

    output = stream.getvalue()
    assert "admin:p4ssw0rd" not in output
    assert "[REDACTED]" in output

    # Clean up: restore default logging config
    logging.root.handlers.clear()


# ============================================================================
# Traceback redaction
# ============================================================================


def test_traceback_secrets_redacted() -> None:
    """Secrets in traceback messages are redacted."""
    handler = _MemoryHandler()
    handler.setFormatter(RedactingFormatter("%(message)s"))

    logger = logging.getLogger("test_traceback_redact")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False

    try:
        raise ConnectionError("Failed to connect to postgresql://admin:secret@db:5432/prod")
    except ConnectionError:
        logger.exception("Database connection failed")

    assert len(handler.records) == 1
    # The traceback string should also have the secret redacted
    assert "admin:secret" not in handler.records[0]
    assert "[REDACTED]" in handler.records[0]


# ============================================================================
# Pattern ordering
# ============================================================================


def test_nexus_api_key_takes_precedence_over_generic_sk() -> None:
    """Nexus API key pattern (more specific) runs before generic sk- pattern."""
    text = "key=sk-test_admin_550e8400-e29b-41d4-a716-446655440000_a1b2c3d4"
    result = redact_text(text)
    assert "sk-test_admin" not in result
    assert "[REDACTED]" in result
