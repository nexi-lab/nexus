"""Tests for Sentry error tracking module.

Issue #759: Sentry for error tracking and performance.

Tests cover setup, shutdown, before_send filtering, traces sampler,
environment variable parsing, sample rate validation, and graceful degradation.

Mirrors the structure of test_telemetry.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.server import sentry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sentry_state():
    """Reset sentry module state before each test."""
    sentry._initialized = False
    sentry._resolved_traces_rate = 0.0
    yield
    sentry._initialized = False
    sentry._resolved_traces_rate = 0.0


@pytest.fixture
def _clean_env(monkeypatch):
    """Remove all SENTRY_ env vars for a clean test."""
    for var in (
        "SENTRY_DSN",
        "SENTRY_ENVIRONMENT",
        "SENTRY_TRACES_SAMPLE_RATE",
        "SENTRY_PROFILES_SAMPLE_RATE",
        "SENTRY_SEND_DEFAULT_PII",
        "NEXUS_ENV",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# is_sentry_enabled
# ---------------------------------------------------------------------------


class TestIsSentryEnabled:
    def test_enabled_with_dsn(self, monkeypatch) -> None:
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")
        assert sentry.is_sentry_enabled() is True

    def test_disabled_without_dsn(self, _clean_env) -> None:
        assert sentry.is_sentry_enabled() is False

    def test_disabled_with_empty_dsn(self, monkeypatch) -> None:
        monkeypatch.setenv("SENTRY_DSN", "")
        assert sentry.is_sentry_enabled() is False

    def test_disabled_with_whitespace_dsn(self, monkeypatch) -> None:
        monkeypatch.setenv("SENTRY_DSN", "   ")
        assert sentry.is_sentry_enabled() is False


# ---------------------------------------------------------------------------
# _parse_sample_rate
# ---------------------------------------------------------------------------


class TestParseSampleRate:
    """Tests for sample rate validation and clamping."""

    def test_valid_rate(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_RATE", "0.5")
        assert sentry._parse_sample_rate("TEST_RATE") == 0.5

    def test_zero_rate(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_RATE", "0.0")
        assert sentry._parse_sample_rate("TEST_RATE") == 0.0

    def test_one_rate(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_RATE", "1.0")
        assert sentry._parse_sample_rate("TEST_RATE") == 1.0

    def test_clamps_above_one(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_RATE", "5.0")
        assert sentry._parse_sample_rate("TEST_RATE") == 1.0

    def test_clamps_below_zero(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_RATE", "-0.5")
        assert sentry._parse_sample_rate("TEST_RATE") == 0.0

    def test_invalid_string_uses_default(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_RATE", "banana")
        assert sentry._parse_sample_rate("TEST_RATE") == 0.0

    def test_invalid_string_uses_custom_default(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_RATE", "not-a-number")
        assert sentry._parse_sample_rate("TEST_RATE", default=0.1) == 0.1

    def test_missing_env_var_uses_default(self, _clean_env) -> None:
        assert sentry._parse_sample_rate("NONEXISTENT_RATE") == 0.0

    def test_missing_env_var_uses_custom_default(self, _clean_env) -> None:
        assert sentry._parse_sample_rate("NONEXISTENT_RATE", default=0.25) == 0.25


# ---------------------------------------------------------------------------
# sentry_before_send
# ---------------------------------------------------------------------------


class TestBeforeSend:
    """Tests for the before_send filtering hook."""

    def test_drops_expected_nexus_error(self) -> None:
        """Expected errors (is_expected=True) should be dropped."""
        from nexus.core.exceptions import NexusFileNotFoundError

        exc = NexusFileNotFoundError("/test/path")
        assert exc.is_expected is True

        event = {"event_id": "abc123", "tags": {}}
        hint = {"exc_info": (type(exc), exc, None)}

        result = sentry.sentry_before_send(event, hint)
        assert result is None  # Dropped

    def test_sends_unexpected_nexus_error(self) -> None:
        """Unexpected errors (is_expected=False) should be sent."""
        from nexus.core.exceptions import BackendError

        exc = BackendError("Storage failure")
        assert exc.is_expected is False

        event = {"event_id": "abc123"}
        hint = {"exc_info": (type(exc), exc, None)}

        result = sentry.sentry_before_send(event, hint)
        assert result is not None
        assert result["event_id"] == "abc123"

    def test_sends_non_nexus_exception(self) -> None:
        """Non-Nexus exceptions (no is_expected attr) should be sent."""
        exc = ValueError("unexpected error")
        assert not hasattr(exc, "is_expected")

        event = {"event_id": "abc123"}
        hint = {"exc_info": (type(exc), exc, None)}

        result = sentry.sentry_before_send(event, hint)
        assert result is not None

    def test_sends_event_without_exception(self) -> None:
        """Events without exceptions in hint should be sent."""
        event = {"event_id": "abc123"}
        hint = {}

        result = sentry.sentry_before_send(event, hint)
        assert result is not None

    def test_sends_event_with_none_exc_info(self) -> None:
        """Events with None exc_info should be sent."""
        event = {"event_id": "abc123"}
        hint = {"exc_info": None}

        result = sentry.sentry_before_send(event, hint)
        assert result is not None

    def test_attaches_correlation_id_when_present(self) -> None:
        """Should attach correlation_id as a tag when available."""
        from nexus.server.middleware.correlation import correlation_id_var

        token = correlation_id_var.set("test-correlation-123")
        try:
            event = {"event_id": "abc123"}
            hint = {}

            result = sentry.sentry_before_send(event, hint)
            assert result is not None
            assert result["tags"]["correlation_id"] == "test-correlation-123"
        finally:
            correlation_id_var.reset(token)

    def test_no_correlation_id_when_absent(self) -> None:
        """Should not add correlation_id tag when not in context."""
        from nexus.server.middleware.correlation import correlation_id_var

        # Ensure no correlation_id is set
        token = correlation_id_var.set(None)
        try:
            event = {"event_id": "abc123"}
            hint = {}

            result = sentry.sentry_before_send(event, hint)
            assert result is not None
            assert "tags" not in result or "correlation_id" not in result.get("tags", {})
        finally:
            correlation_id_var.reset(token)

    def test_drops_expected_permission_error(self) -> None:
        """Permission denied errors are expected and should be dropped."""
        from nexus.core.exceptions import PermissionDeniedError

        exc = PermissionDeniedError("No access")
        assert exc.is_expected is True

        event = {"event_id": "abc123"}
        hint = {"exc_info": (type(exc), exc, None)}

        result = sentry.sentry_before_send(event, hint)
        assert result is None

    def test_sends_base_nexus_error(self) -> None:
        """Base NexusError is unexpected by default and should be sent."""
        from nexus.core.exceptions import NexusError

        exc = NexusError("Unknown failure")
        assert exc.is_expected is False

        event = {"event_id": "abc123"}
        hint = {"exc_info": (type(exc), exc, None)}

        result = sentry.sentry_before_send(event, hint)
        assert result is not None


# ---------------------------------------------------------------------------
# _sentry_traces_sampler
# ---------------------------------------------------------------------------


class TestTracesSampler:
    """Tests for the custom traces sampler.

    The sampler reads _resolved_traces_rate (set by setup_sentry),
    not the env var directly.
    """

    def test_returns_zero_when_rate_is_zero(self) -> None:
        sentry._resolved_traces_rate = 0.0
        ctx = {"transaction_context": {"name": "/api/v1/files", "op": "http.server"}}
        assert sentry._sentry_traces_sampler(ctx) == 0.0

    def test_returns_configured_rate_for_normal_endpoint(self) -> None:
        sentry._resolved_traces_rate = 0.5
        ctx = {"transaction_context": {"name": "/api/v1/files", "op": "http.server"}}
        assert sentry._sentry_traces_sampler(ctx) == 0.5

    def test_drops_health_check(self) -> None:
        sentry._resolved_traces_rate = 1.0
        ctx = {"transaction_context": {"name": "/health", "op": "http.server"}}
        assert sentry._sentry_traces_sampler(ctx) == 0.0

    def test_drops_metrics_endpoint(self) -> None:
        sentry._resolved_traces_rate = 1.0
        ctx = {"transaction_context": {"name": "/metrics/pool", "op": "http.server"}}
        assert sentry._sentry_traces_sampler(ctx) == 0.0

    def test_drops_favicon(self) -> None:
        sentry._resolved_traces_rate = 1.0
        ctx = {"transaction_context": {"name": "/favicon.ico", "op": "http.server"}}
        assert sentry._sentry_traces_sampler(ctx) == 0.0

    def test_drops_options_request_asgi(self) -> None:
        sentry._resolved_traces_rate = 1.0
        ctx = {
            "transaction_context": {"name": "/api/v1/files", "op": "http.server"},
            "asgi_scope": {"method": "OPTIONS"},
        }
        assert sentry._sentry_traces_sampler(ctx) == 0.0

    def test_drops_options_request_wsgi(self) -> None:
        sentry._resolved_traces_rate = 1.0
        ctx = {
            "transaction_context": {"name": "/api/v1/files", "op": "http.server"},
            "wsgi_environ": {"REQUEST_METHOD": "OPTIONS"},
        }
        assert sentry._sentry_traces_sampler(ctx) == 0.0

    def test_default_rate_is_zero(self) -> None:
        # _resolved_traces_rate defaults to 0.0 (from fixture reset)
        ctx = {"transaction_context": {"name": "/api/v1/files", "op": "http.server"}}
        assert sentry._sentry_traces_sampler(ctx) == 0.0

    def test_empty_context_uses_configured_rate(self) -> None:
        sentry._resolved_traces_rate = 0.3
        ctx = {}
        assert sentry._sentry_traces_sampler(ctx) == 0.3


# ---------------------------------------------------------------------------
# setup_sentry
# ---------------------------------------------------------------------------


class TestSetupSentry:
    def test_returns_false_when_no_dsn(self, _clean_env) -> None:
        result = sentry.setup_sentry()
        assert result is False
        assert sentry._initialized is False

    def test_returns_false_when_already_initialized(self, monkeypatch) -> None:
        sentry._initialized = True
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")
        result = sentry.setup_sentry()
        assert result is False

    def test_returns_true_when_dsn_provided(self, monkeypatch) -> None:
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")

        mock_sentry_sdk = MagicMock()
        mock_fastapi_int = MagicMock()
        mock_starlette_int = MagicMock()
        mock_logging_int = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(
                    FastApiIntegration=mock_fastapi_int,
                ),
                "sentry_sdk.integrations.starlette": MagicMock(
                    StarletteIntegration=mock_starlette_int,
                ),
                "sentry_sdk.integrations.logging": MagicMock(
                    LoggingIntegration=mock_logging_int,
                ),
            },
        ):
            result = sentry.setup_sentry()

        assert result is True
        assert sentry._initialized is True
        mock_sentry_sdk.init.assert_called_once()

    def test_returns_false_when_sdk_not_installed(self, monkeypatch) -> None:
        monkeypatch.setenv("SENTRY_DSN", "https://key@sentry.io/123")

        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def mock_import(name, *args, **kwargs):
            if name.startswith("sentry_sdk"):
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            sentry._initialized = False
            result = sentry.setup_sentry()

        assert result is False

    def test_uses_nexus_env_as_fallback(self, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_ENV", "staging")
        monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)

        mock_sentry_sdk = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(
                    FastApiIntegration=MagicMock(),
                ),
                "sentry_sdk.integrations.starlette": MagicMock(
                    StarletteIntegration=MagicMock(),
                ),
                "sentry_sdk.integrations.logging": MagicMock(
                    LoggingIntegration=MagicMock(),
                ),
            },
        ):
            sentry.setup_sentry(dsn="https://key@sentry.io/123")

        call_kwargs = mock_sentry_sdk.init.call_args
        assert call_kwargs is not None
        assert call_kwargs[1]["environment"] == "staging"

    def test_parameter_overrides_env_vars(self, monkeypatch) -> None:
        monkeypatch.setenv("SENTRY_DSN", "https://env@sentry.io/1")
        monkeypatch.setenv("SENTRY_ENVIRONMENT", "production")

        mock_sentry_sdk = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(
                    FastApiIntegration=MagicMock(),
                ),
                "sentry_sdk.integrations.starlette": MagicMock(
                    StarletteIntegration=MagicMock(),
                ),
                "sentry_sdk.integrations.logging": MagicMock(
                    LoggingIntegration=MagicMock(),
                ),
            },
        ):
            sentry.setup_sentry(
                dsn="https://override@sentry.io/2",
                environment="custom-env",
            )

        call_kwargs = mock_sentry_sdk.init.call_args
        assert call_kwargs is not None
        assert call_kwargs[1]["dsn"] == "https://override@sentry.io/2"
        assert call_kwargs[1]["environment"] == "custom-env"

    def test_traces_rate_parameter_overrides_env(self, monkeypatch) -> None:
        """Parameter traces_sample_rate should override env var."""
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")

        mock_sentry_sdk = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(FastApiIntegration=MagicMock()),
                "sentry_sdk.integrations.starlette": MagicMock(StarletteIntegration=MagicMock()),
                "sentry_sdk.integrations.logging": MagicMock(LoggingIntegration=MagicMock()),
            },
        ):
            sentry.setup_sentry(dsn="https://key@sentry.io/123", traces_sample_rate=0.8)

        assert sentry._resolved_traces_rate == 0.8

    def test_pii_env_var_true(self, monkeypatch) -> None:
        """SENTRY_SEND_DEFAULT_PII=true should enable PII."""
        monkeypatch.setenv("SENTRY_SEND_DEFAULT_PII", "true")

        mock_sentry_sdk = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(FastApiIntegration=MagicMock()),
                "sentry_sdk.integrations.starlette": MagicMock(StarletteIntegration=MagicMock()),
                "sentry_sdk.integrations.logging": MagicMock(LoggingIntegration=MagicMock()),
            },
        ):
            sentry.setup_sentry(dsn="https://key@sentry.io/123")

        call_kwargs = mock_sentry_sdk.init.call_args[1]
        assert call_kwargs["send_default_pii"] is True

    def test_pii_env_var_false(self, monkeypatch) -> None:
        """SENTRY_SEND_DEFAULT_PII=false should disable PII."""
        monkeypatch.setenv("SENTRY_SEND_DEFAULT_PII", "false")

        mock_sentry_sdk = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(FastApiIntegration=MagicMock()),
                "sentry_sdk.integrations.starlette": MagicMock(StarletteIntegration=MagicMock()),
                "sentry_sdk.integrations.logging": MagicMock(LoggingIntegration=MagicMock()),
            },
        ):
            sentry.setup_sentry(dsn="https://key@sentry.io/123")

        call_kwargs = mock_sentry_sdk.init.call_args[1]
        assert call_kwargs["send_default_pii"] is False

    def test_pii_parameter_overrides_env(self, monkeypatch) -> None:
        """Parameter send_default_pii should override env var."""
        monkeypatch.setenv("SENTRY_SEND_DEFAULT_PII", "false")

        mock_sentry_sdk = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(FastApiIntegration=MagicMock()),
                "sentry_sdk.integrations.starlette": MagicMock(StarletteIntegration=MagicMock()),
                "sentry_sdk.integrations.logging": MagicMock(LoggingIntegration=MagicMock()),
            },
        ):
            sentry.setup_sentry(dsn="https://key@sentry.io/123", send_default_pii=True)

        call_kwargs = mock_sentry_sdk.init.call_args[1]
        assert call_kwargs["send_default_pii"] is True

    def test_invalid_traces_sample_rate_uses_default(self, monkeypatch) -> None:
        """Invalid SENTRY_TRACES_SAMPLE_RATE should gracefully use 0.0."""
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "banana")

        mock_sentry_sdk = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sentry_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(FastApiIntegration=MagicMock()),
                "sentry_sdk.integrations.starlette": MagicMock(StarletteIntegration=MagicMock()),
                "sentry_sdk.integrations.logging": MagicMock(LoggingIntegration=MagicMock()),
            },
        ):
            sentry.setup_sentry(dsn="https://key@sentry.io/123")

        # Should not crash, and rate should fall back to 0.0
        assert sentry._resolved_traces_rate == 0.0


# ---------------------------------------------------------------------------
# shutdown_sentry
# ---------------------------------------------------------------------------


class TestShutdownSentry:
    def test_noop_when_not_initialized(self) -> None:
        sentry.shutdown_sentry()
        assert sentry._initialized is False

    def test_flushes_and_clears_state(self) -> None:
        sentry._initialized = True
        sentry._resolved_traces_rate = 0.5

        mock_sentry_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sentry_sdk}):
            sentry.shutdown_sentry()

        assert sentry._initialized is False
        assert sentry._resolved_traces_rate == 0.0
        mock_sentry_sdk.flush.assert_called_once_with(timeout=2.0)

    def test_handles_flush_error(self) -> None:
        sentry._initialized = True
        sentry._resolved_traces_rate = 0.5

        mock_sentry_sdk = MagicMock()
        mock_sentry_sdk.flush.side_effect = RuntimeError("flush failed")
        with patch.dict("sys.modules", {"sentry_sdk": mock_sentry_sdk}):
            sentry.shutdown_sentry()

        # State should still be cleared even on error
        assert sentry._initialized is False
        assert sentry._resolved_traces_rate == 0.0


# ---------------------------------------------------------------------------
# get_nexus_version (moved to nexus.server._version)
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_returns_version_string(self) -> None:
        from nexus.server._version import get_nexus_version

        result = get_nexus_version()
        assert isinstance(result, str)

    def test_returns_unknown_on_error(self) -> None:
        from nexus.server._version import get_nexus_version

        with patch("importlib.metadata.version", side_effect=Exception("not installed")):
            result = get_nexus_version()
        assert result == "unknown"
