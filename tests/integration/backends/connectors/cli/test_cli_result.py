"""Tests for CLIResult and CLIErrorMapper (Issue #3148, Decisions #6 + #6A).

Tests cover:
- CLIResult status classification and summary formatting
- CLIErrorMapper pattern matching against default and custom patterns
- All 5 failure modes: not installed, exit code, bad output, timeout, auth expired
- Edge cases: empty stderr, no match, multiple patterns, priority ordering
"""

from nexus.backends.connectors.cli.result import (
    CLIErrorMapper,
    CLIResult,
    CLIResultStatus,
    ErrorMapping,
)

# ---------------------------------------------------------------------------
# CLIResult
# ---------------------------------------------------------------------------


class TestCLIResult:
    def test_success_result(self) -> None:
        result = CLIResult(status=CLIResultStatus.SUCCESS, exit_code=0, stdout='{"ok": true}')
        assert result.ok is True
        assert "OK" in result.summary()

    def test_exit_error_result(self) -> None:
        result = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            stderr="command not found",
            command=["gws", "gmail", "+send"],
        )
        assert result.ok is False
        assert "exit=1" in result.summary()
        assert "command not found" in result.summary()

    def test_not_installed_result(self) -> None:
        result = CLIResult(status=CLIResultStatus.NOT_INSTALLED, command=["gws"])
        assert result.ok is False
        assert result.status == CLIResultStatus.NOT_INSTALLED

    def test_timeout_result(self) -> None:
        result = CLIResult(
            status=CLIResultStatus.TIMEOUT,
            command=["gws", "gmail", "messages.list"],
            duration_ms=30000.0,
        )
        assert result.ok is False
        assert result.status == CLIResultStatus.TIMEOUT

    def test_auth_expired_result(self) -> None:
        result = CLIResult(
            status=CLIResultStatus.AUTH_EXPIRED,
            exit_code=1,
            stderr="401 Unauthorized",
            retryable=True,
        )
        assert result.ok is False
        assert result.retryable is True

    def test_bad_output_result(self) -> None:
        result = CLIResult(
            status=CLIResultStatus.BAD_OUTPUT,
            exit_code=0,
            stdout="<html>not json</html>",
        )
        assert result.ok is False

    def test_summary_truncates_long_stderr(self) -> None:
        long_stderr = "x" * 200
        result = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            stderr=long_stderr,
            command=["gws"],
        )
        summary = result.summary()
        assert len(summary) < 200  # Truncated

    def test_summary_with_error_code(self) -> None:
        result = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            error_code="RATE_LIMITED",
            command=["gws"],
        )
        assert "RATE_LIMITED" in result.summary()


# ---------------------------------------------------------------------------
# CLIErrorMapper — default patterns
# ---------------------------------------------------------------------------


class TestCLIErrorMapper:
    def setup_method(self) -> None:
        self.mapper = CLIErrorMapper()

    def test_success_returns_none(self) -> None:
        assert self.mapper.classify(exit_code=0, stderr="") is None

    def test_rate_limit_429(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="429 Too Many Requests")
        assert result is not None
        assert result.code == "RATE_LIMITED"
        assert result.retryable is True
        assert result.backoff == "exponential"

    def test_rate_limit_text(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="Error: rate limit exceeded")
        assert result is not None
        assert result.code == "RATE_LIMITED"

    def test_auth_expired_401(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="401 Unauthorized")
        assert result is not None
        assert result.code == "AUTH_EXPIRED"
        assert result.retryable is True
        assert result.action == "reauth"

    def test_auth_expired_token(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="token expired, please re-authenticate")
        assert result is not None
        assert result.code == "AUTH_EXPIRED"

    def test_permission_denied(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="403 Forbidden")
        assert result is not None
        assert result.code == "PERMISSION_DENIED"
        assert result.retryable is False

    def test_not_found(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="404 Not Found")
        assert result is not None
        assert result.code == "NOT_FOUND"
        assert result.retryable is False

    def test_conflict(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="409 Conflict: already exists")
        assert result is not None
        assert result.code == "CONFLICT"

    def test_server_error(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="500 Internal Server Error")
        assert result is not None
        assert result.code == "SERVER_ERROR"
        assert result.retryable is True

    def test_timeout_pattern(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="Error: deadline exceeded")
        assert result is not None
        assert result.code == "TIMEOUT"

    def test_network_error(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="connection refused")
        assert result is not None
        assert result.code == "NETWORK_ERROR"
        assert result.retryable is True

    def test_no_match_returns_none(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="something unknown happened")
        assert result is None

    def test_empty_stderr_no_match(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="")
        assert result is None

    def test_stdout_fallback(self) -> None:
        """Pattern matching checks stdout when stderr is empty."""
        result = self.mapper.classify(exit_code=1, stderr="", stdout="Error: rate limit hit")
        assert result is not None
        assert result.code == "RATE_LIMITED"

    def test_case_insensitive(self) -> None:
        result = self.mapper.classify(exit_code=1, stderr="UNAUTHORIZED")
        assert result is not None
        assert result.code == "AUTH_EXPIRED"


# ---------------------------------------------------------------------------
# CLIErrorMapper — custom patterns
# ---------------------------------------------------------------------------


class TestCLIErrorMapperCustom:
    def test_custom_pattern_takes_priority(self) -> None:
        mapper = CLIErrorMapper(
            extra_patterns=[
                (r"mailbox.full", ErrorMapping(code="MAILBOX_FULL", retryable=False)),
            ]
        )
        result = mapper.classify(exit_code=1, stderr="Error: mailbox full")
        assert result is not None
        assert result.code == "MAILBOX_FULL"

    def test_custom_pattern_before_default(self) -> None:
        """Custom patterns are checked before defaults."""
        mapper = CLIErrorMapper(
            extra_patterns=[
                (r"429", ErrorMapping(code="CUSTOM_RATE_LIMIT", retryable=True)),
            ]
        )
        result = mapper.classify(exit_code=1, stderr="429 Too Many Requests")
        assert result is not None
        assert result.code == "CUSTOM_RATE_LIMIT"  # Custom wins

    def test_classify_result_enriches(self) -> None:
        mapper = CLIErrorMapper()
        raw = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            stderr="401 Unauthorized",
            command=["gws", "gmail", "+send"],
        )
        enriched = mapper.classify_result(raw)
        assert enriched.error_code == "AUTH_EXPIRED"
        assert enriched.retryable is True
        assert enriched.status == CLIResultStatus.AUTH_EXPIRED

    def test_classify_result_success_unchanged(self) -> None:
        mapper = CLIErrorMapper()
        raw = CLIResult(status=CLIResultStatus.SUCCESS, exit_code=0)
        enriched = mapper.classify_result(raw)
        assert enriched is raw  # Same object, no enrichment

    def test_classify_result_no_match_unchanged(self) -> None:
        mapper = CLIErrorMapper()
        raw = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            stderr="unknown error xyz",
            command=["gws"],
        )
        enriched = mapper.classify_result(raw)
        assert enriched is raw  # No pattern match, same object

    def test_to_dict_serialization(self) -> None:
        mapper = CLIErrorMapper(
            extra_patterns=[
                (r"custom", ErrorMapping(code="CUSTOM", retryable=True, backoff="linear")),
            ]
        )
        exported = mapper.to_dict()
        assert len(exported) > 0
        first = exported[0]
        assert first["code"] == "CUSTOM"
        assert first["retryable"] is True
