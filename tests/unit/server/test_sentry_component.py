"""Tests for Sentry setup/shutdown (mocked SDK).

Issue #2072: Fill coverage gap for Sentry lifecycle.
"""

from unittest.mock import MagicMock, patch


class TestSetupSentry:
    """Tests for setup_sentry() function."""

    def test_setup_sentry_disabled_without_dsn(self) -> None:
        from nexus.server.sentry import setup_sentry

        with patch.dict("os.environ", {}, clear=True):
            # Reset global state
            import nexus.server.sentry as sentry_mod

            sentry_mod._initialized = False
            sentry_mod._resolved_traces_rate = 0.0

            result = setup_sentry()
            assert result is False

    def test_setup_sentry_idempotent(self) -> None:
        import nexus.server.sentry as sentry_mod

        # Simulate already initialized
        sentry_mod._initialized = True
        try:
            result = sentry_mod.setup_sentry(dsn="https://test@sentry.io/1")
            assert result is False
        finally:
            sentry_mod._initialized = False

    def test_setup_sentry_calls_init_with_correct_args(self) -> None:
        import nexus.server.sentry as sentry_mod

        sentry_mod._initialized = False
        sentry_mod._resolved_traces_rate = 0.0

        mock_sdk = MagicMock()
        mock_fastapi = MagicMock()
        mock_starlette = MagicMock()
        mock_logging_int = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "sentry_sdk": mock_sdk,
                "sentry_sdk.integrations": MagicMock(),
                "sentry_sdk.integrations.fastapi": MagicMock(FastApiIntegration=mock_fastapi),
                "sentry_sdk.integrations.starlette": MagicMock(StarletteIntegration=mock_starlette),
                "sentry_sdk.integrations.logging": MagicMock(LoggingIntegration=mock_logging_int),
                "sentry_sdk.types": MagicMock(),
            },
        ):
            try:
                result = sentry_mod.setup_sentry(
                    dsn="https://test@sentry.io/1",
                    environment="test",
                    traces_sample_rate=0.5,
                )
                assert result is True
                mock_sdk.init.assert_called_once()
                call_kwargs = mock_sdk.init.call_args
                assert call_kwargs.kwargs["dsn"] == "https://test@sentry.io/1"
                assert call_kwargs.kwargs["environment"] == "test"
            finally:
                sentry_mod._initialized = False
                sentry_mod._resolved_traces_rate = 0.0


class TestSentryBeforeSend:
    """Tests for sentry_before_send() filter."""

    def test_before_send_drops_expected_errors(self) -> None:
        from nexus.server.sentry import sentry_before_send

        class ExpectedError(Exception):
            is_expected = True

        exc = ExpectedError("not found")
        event: dict = {"tags": {}}
        hint = {"exc_info": (type(exc), exc, None)}

        result = sentry_before_send(event, hint)  # type: ignore[arg-type]
        assert result is None

    def test_before_send_attaches_correlation_id(self) -> None:
        from nexus.server.sentry import sentry_before_send

        event: dict = {}
        hint: dict = {}

        mock_var = MagicMock()
        mock_var.get.return_value = "req-123"
        with patch("nexus.server.middleware.correlation.correlation_id_var", mock_var):
            result = sentry_before_send(event, hint)  # type: ignore[arg-type]

        assert result is not None
        assert result["tags"]["correlation_id"] == "req-123"

    def test_before_send_passes_through_normal_errors(self) -> None:
        from nexus.server.sentry import sentry_before_send

        exc = RuntimeError("unexpected")
        event: dict = {"tags": {}}
        hint = {"exc_info": (type(exc), exc, None)}

        # Patch correlation module to raise ImportError
        with patch.dict("sys.modules", {"nexus.server.middleware.correlation": None}):
            result = sentry_before_send(event, hint)  # type: ignore[arg-type]

        assert result is not None


class TestShutdownSentry:
    """Tests for shutdown_sentry() function."""

    def test_shutdown_sentry_flushes(self) -> None:
        import nexus.server.sentry as sentry_mod

        sentry_mod._initialized = True

        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            sentry_mod.shutdown_sentry()
            mock_sdk.flush.assert_called_once_with(timeout=2.0)

        assert sentry_mod._initialized is False

    def test_shutdown_sentry_idempotent(self) -> None:
        import nexus.server.sentry as sentry_mod

        sentry_mod._initialized = False

        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            sentry_mod.shutdown_sentry()
            mock_sdk.flush.assert_not_called()
