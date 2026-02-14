"""Tests for Sentry error tracking module.

Issue #759: Sentry for Error Tracking and Performance.

Tests cover:
- ``is_sentry_enabled()``: DSN presence check
- ``_build_config_from_env()``: Environment variable parsing
- ``setup_sentry()``: SDK initialization (mocked)
- ``_before_send()``: Correlation ID tagging + expected error filtering
- ``_traces_sampler()``: Health check exclusion + configurable rate
- ``shutdown_sentry()``: Flush + reset
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# is_sentry_enabled
# ---------------------------------------------------------------------------


class TestIsSentryEnabled:
    """Tests for DSN presence check."""

    def test_enabled_when_dsn_set(self) -> None:
        with patch.dict("os.environ", {"SENTRY_DSN": "https://key@sentry.io/123"}):
            from nexus.server.sentry import is_sentry_enabled

            assert is_sentry_enabled() is True

    def test_disabled_when_dsn_empty(self) -> None:
        with patch.dict("os.environ", {"SENTRY_DSN": ""}, clear=False):
            from nexus.server.sentry import is_sentry_enabled

            assert is_sentry_enabled() is False

    def test_disabled_when_dsn_not_set(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SENTRY_DSN", None)
            from nexus.server.sentry import is_sentry_enabled

            assert is_sentry_enabled() is False

    def test_disabled_when_dsn_whitespace(self) -> None:
        with patch.dict("os.environ", {"SENTRY_DSN": "   "}):
            from nexus.server.sentry import is_sentry_enabled

            assert is_sentry_enabled() is False


# ---------------------------------------------------------------------------
# _build_config_from_env
# ---------------------------------------------------------------------------


class TestBuildConfigFromEnv:
    """Tests for environment variable parsing into SentryConfig."""

    def test_default_values(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            # Clear all SENTRY_ vars
            for key in list(os.environ):
                if key.startswith("SENTRY_"):
                    os.environ.pop(key)

            from nexus.server.sentry import _build_config_from_env

            config = _build_config_from_env()
            assert config.dsn == ""
            assert config.environment == "development"
            assert config.traces_sample_rate == 0.1
            assert config.profiles_sample_rate == 0.0
            assert config.send_default_pii is False
            assert config.debug is False

    def test_all_env_vars(self) -> None:
        env = {
            "SENTRY_DSN": "https://key@sentry.io/99",
            "SENTRY_ENVIRONMENT": "production",
            "SENTRY_TRACES_SAMPLE_RATE": "0.5",
            "SENTRY_PROFILES_SAMPLE_RATE": "0.2",
            "SENTRY_SEND_PII": "true",
            "SENTRY_DEBUG": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            from nexus.server.sentry import _build_config_from_env

            config = _build_config_from_env()
            assert config.dsn == "https://key@sentry.io/99"
            assert config.environment == "production"
            assert config.traces_sample_rate == 0.5
            assert config.profiles_sample_rate == 0.2
            assert config.send_default_pii is True
            assert config.debug is True


# ---------------------------------------------------------------------------
# setup_sentry
# ---------------------------------------------------------------------------


class TestSetupSentry:
    """Tests for SDK initialization."""

    def test_returns_false_without_dsn(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SENTRY_DSN", None)

            import nexus.server.sentry as mod

            mod._initialized = False
            assert mod.setup_sentry() is False

    def test_returns_false_when_already_initialized(self) -> None:
        import nexus.server.sentry as mod

        mod._initialized = True
        try:
            assert mod.setup_sentry() is False
        finally:
            mod._initialized = False

    @patch("nexus.server.sentry.sentry_sdk", create=True)
    def test_calls_sentry_init_with_dsn(self, mock_sdk: MagicMock) -> None:
        """When DSN is provided, sentry_sdk.init() is called."""
        import nexus.server.sentry as mod
        from nexus.core.config import SentryConfig

        mod._initialized = False

        config = SentryConfig(dsn="https://key@sentry.io/1", environment="test")

        # Mock the imports inside setup_sentry
        mock_init = MagicMock()
        mock_logging_integration = MagicMock()
        mock_fastapi_integration = MagicMock()
        mock_starlette_integration = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sdk,
                "sentry_sdk.integrations.fastapi": MagicMock(
                    FastApiIntegration=mock_fastapi_integration,
                ),
                "sentry_sdk.integrations.logging": MagicMock(
                    LoggingIntegration=mock_logging_integration,
                ),
                "sentry_sdk.integrations.starlette": MagicMock(
                    StarletteIntegration=mock_starlette_integration,
                ),
            },
        ):
            mock_sdk.init = mock_init
            result = mod.setup_sentry(config=config)

        assert result is True
        assert mod._initialized is True
        mock_init.assert_called_once()

        # Verify LoggingIntegration is disabled
        call_kwargs = mock_init.call_args
        integrations = call_kwargs.kwargs.get("integrations") or call_kwargs[1].get(
            "integrations", []
        )
        assert len(integrations) >= 1  # At least LoggingIntegration

        # Cleanup
        mod._initialized = False


# ---------------------------------------------------------------------------
# _before_send
# ---------------------------------------------------------------------------


class TestBeforeSend:
    """Tests for the before_send hook."""

    def test_attaches_correlation_id_tag(self) -> None:
        from nexus.server.middleware.correlation import correlation_id_var
        from nexus.server.sentry import _before_send

        token = correlation_id_var.set("test-corr-id-123")
        try:
            event: dict = {"tags": {}}
            result = _before_send(event, {})
            assert result is not None
            assert result["tags"]["correlation_id"] == "test-corr-id-123"
        finally:
            correlation_id_var.reset(token)

    def test_creates_tags_dict_if_missing(self) -> None:
        from nexus.server.middleware.correlation import correlation_id_var
        from nexus.server.sentry import _before_send

        token = correlation_id_var.set("abc")
        try:
            event: dict = {}
            result = _before_send(event, {})
            assert result is not None
            assert result["tags"]["correlation_id"] == "abc"
        finally:
            correlation_id_var.reset(token)

    def test_drops_expected_errors(self) -> None:
        from nexus.server.sentry import _before_send

        class ExpectedError(Exception):
            is_expected = True

        exc = ExpectedError("not found")
        hint = {"exc_info": (type(exc), exc, None)}

        result = _before_send({"tags": {}}, hint)
        assert result is None

    def test_passes_unexpected_errors(self) -> None:
        from nexus.server.sentry import _before_send

        class UnexpectedError(Exception):
            is_expected = False

        exc = UnexpectedError("server error")
        hint = {"exc_info": (type(exc), exc, None)}

        result = _before_send({"tags": {}}, hint)
        assert result is not None

    def test_passes_errors_without_is_expected(self) -> None:
        from nexus.server.sentry import _before_send

        exc = RuntimeError("generic error")
        hint = {"exc_info": (type(exc), exc, None)}

        result = _before_send({"tags": {}}, hint)
        assert result is not None

    def test_passes_event_without_exc_info(self) -> None:
        from nexus.server.sentry import _before_send

        result = _before_send({"message": "log message"}, {})
        assert result is not None


# ---------------------------------------------------------------------------
# _traces_sampler
# ---------------------------------------------------------------------------


class TestTracesSampler:
    """Tests for the custom traces sampler."""

    def test_skips_health_endpoint(self) -> None:
        from nexus.server.sentry import _traces_sampler

        ctx = {"asgi_scope": {"path": "/health"}}
        assert _traces_sampler(ctx) == 0.0

    def test_skips_health_detailed_endpoint(self) -> None:
        from nexus.server.sentry import _traces_sampler

        ctx = {"asgi_scope": {"path": "/health/detailed"}}
        assert _traces_sampler(ctx) == 0.0

    def test_returns_configured_rate_for_normal_endpoints(self) -> None:
        from nexus.server.sentry import _traces_sampler

        with patch.dict("os.environ", {"SENTRY_TRACES_SAMPLE_RATE": "0.3"}):
            ctx = {"asgi_scope": {"path": "/api/files"}}
            assert _traces_sampler(ctx) == 0.3

    def test_returns_default_rate_when_no_env(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SENTRY_TRACES_SAMPLE_RATE", None)

            from nexus.server.sentry import _traces_sampler

            ctx = {"asgi_scope": {"path": "/api/files"}}
            assert _traces_sampler(ctx) == 0.1

    def test_handles_empty_sampling_context(self) -> None:
        from nexus.server.sentry import _traces_sampler

        with patch.dict("os.environ", {"SENTRY_TRACES_SAMPLE_RATE": "0.5"}):
            result = _traces_sampler({})
            assert result == 0.5


# ---------------------------------------------------------------------------
# shutdown_sentry
# ---------------------------------------------------------------------------


class TestShutdownSentry:
    """Tests for Sentry shutdown."""

    def test_noop_when_not_initialized(self) -> None:
        import nexus.server.sentry as mod

        mod._initialized = False
        # Should not raise
        mod.shutdown_sentry()
        assert mod._initialized is False

    def test_flushes_when_initialized(self) -> None:
        import nexus.server.sentry as mod

        mod._initialized = True
        mock_client = MagicMock()
        mock_client.is_active.return_value = True

        mock_sdk = MagicMock()
        mock_sdk.get_client.return_value = mock_client

        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            mod.shutdown_sentry()

        mock_sdk.flush.assert_called_once_with(timeout=2.0)
        mock_client.close.assert_called_once()
        assert mod._initialized is False


# ---------------------------------------------------------------------------
# _get_release
# ---------------------------------------------------------------------------


class TestGetRelease:
    """Tests for release version string."""

    def test_returns_nexus_prefix(self) -> None:
        from nexus.server.sentry import _get_release

        release = _get_release()
        assert release.startswith("nexus@")

    def test_fallback_on_error(self) -> None:
        from nexus.server.sentry import _get_release

        with patch("importlib.metadata.version", side_effect=Exception("no package")):
            release = _get_release()

        assert release == "nexus@unknown"
