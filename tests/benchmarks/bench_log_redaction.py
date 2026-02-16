"""Benchmark tests for log redaction performance (Issue #86).

Measures RedactingFormatter overhead to ensure secret masking
doesn't impact server latency. Target: < 1ms per log line.

Run with: pytest tests/benchmarks/bench_log_redaction.py -v
"""

import logging

import pytest

from nexus.core.logging_utils import RedactingFormatter, redact_text

# --- Test data ---

# A typical log line with no secrets (common case, ~95% of logs)
CLEAN_LINE = (
    "2024-01-15 12:00:00 - nexus.core.permissions - INFO - "
    "Permission check passed for user alice on /workspace/files/report.pdf"
)

# A log line with one secret (uncommon but must be handled)
SECRET_LINE = (
    "2024-01-15 12:00:00 - nexus.auth - WARNING - "
    "Auth failed: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
)

# A log line with multiple secrets (worst case)
MULTI_SECRET_LINE = (
    "2024-01-15 12:00:00 - nexus.sandbox - ERROR - "
    "Mount failed: nexus_url=postgresql://admin:secret@db:5432/prod, "
    "api_key=sk-abcdefghijklmnopqrstuvwxyz12345, "
    "password=my_super_secret_password_value"
)


@pytest.mark.benchmark(group="redaction")
def test_benchmark_redact_clean_line(benchmark: pytest.fixture) -> None:
    """Benchmark: redacting a clean line (no secrets) — common case."""
    result = benchmark(redact_text, CLEAN_LINE)
    assert "[REDACTED]" not in result


@pytest.mark.benchmark(group="redaction")
def test_benchmark_redact_one_secret(benchmark: pytest.fixture) -> None:
    """Benchmark: redacting a line with one secret."""
    result = benchmark(redact_text, SECRET_LINE)
    assert "[REDACTED]" in result


@pytest.mark.benchmark(group="redaction")
def test_benchmark_redact_multi_secret(benchmark: pytest.fixture) -> None:
    """Benchmark: redacting a line with multiple secrets — worst case."""
    result = benchmark(redact_text, MULTI_SECRET_LINE)
    assert result.count("[REDACTED]") >= 3


@pytest.mark.benchmark(group="formatter")
def test_benchmark_formatter_clean(benchmark: pytest.fixture) -> None:
    """Benchmark: full RedactingFormatter.format() on a clean message."""
    formatter = RedactingFormatter("%(name)s - %(levelname)s - %(message)s")
    record = logging.LogRecord(
        name="nexus.core",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="File uploaded: /workspace/report.pdf (1024 bytes)",
        args=None,
        exc_info=None,
    )
    result = benchmark(formatter.format, record)
    assert "[REDACTED]" not in result


@pytest.mark.benchmark(group="formatter")
def test_benchmark_formatter_with_secret(benchmark: pytest.fixture) -> None:
    """Benchmark: full RedactingFormatter.format() with a secret."""
    formatter = RedactingFormatter("%(name)s - %(levelname)s - %(message)s")
    record = logging.LogRecord(
        name="nexus.auth",
        level=logging.WARNING,
        pathname="test.py",
        lineno=1,
        msg="DB: postgresql://admin:secret@host:5432/prod",
        args=None,
        exc_info=None,
    )
    result = benchmark(formatter.format, record)
    assert "[REDACTED]" in result
