"""Tests for Pyroscope continuous profiling module.

Issue #763: Continuous profiling via Grafana Pyroscope.

Tests cover setup, shutdown, environment variable parsing,
trace-to-profile OTel integration, and graceful degradation.

Mirrors the structure of test_sentry.py and test_telemetry.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.server import profiling

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_profiling_state():
    """Reset profiling module state before each test."""
    profiling._initialized = False
    yield
    profiling._initialized = False


@pytest.fixture
def _clean_env(monkeypatch):
    """Remove all PYROSCOPE_ env vars for a clean test."""
    for var in (
        "PYROSCOPE_ENABLED",
        "PYROSCOPE_SERVER_ADDRESS",
        "PYROSCOPE_APPLICATION_NAME",
        "PYROSCOPE_SAMPLE_RATE",
        "PYROSCOPE_AUTH_TOKEN",
        "NEXUS_ENV",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# is_profiling_enabled
# ---------------------------------------------------------------------------


class TestIsProfilingEnabled:
    def test_enabled_true(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
        assert profiling.is_profiling_enabled() is True

    def test_enabled_1(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "1")
        assert profiling.is_profiling_enabled() is True

    def test_enabled_yes(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "yes")
        assert profiling.is_profiling_enabled() is True

    def test_disabled_false(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "false")
        assert profiling.is_profiling_enabled() is False

    def test_disabled_default(self, _clean_env) -> None:
        assert profiling.is_profiling_enabled() is False

    def test_disabled_random_string(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "maybe")
        assert profiling.is_profiling_enabled() is False


# ---------------------------------------------------------------------------
# setup_profiling
# ---------------------------------------------------------------------------


class TestSetupProfiling:
    def test_disabled_returns_false(self, _clean_env) -> None:
        """When PYROSCOPE_ENABLED is not set, setup returns False."""
        assert profiling.setup_profiling() is False

    def test_disabled_explicit_false(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "false")
        assert profiling.setup_profiling() is False

    def test_import_error_returns_false(self, monkeypatch) -> None:
        """When pyroscope-io is not installed, setup returns False gracefully."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
        with patch.dict("sys.modules", {"pyroscope": None}):
            assert profiling.setup_profiling() is False

    def test_success_with_mock(self, monkeypatch) -> None:
        """When pyroscope is available, setup returns True."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
        monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://localhost:4040")

        mock_pyroscope = MagicMock()
        with (
            patch.dict("sys.modules", {"pyroscope": mock_pyroscope}),
            patch.object(profiling, "_register_otel_processor"),
        ):
            result = profiling.setup_profiling()

        assert result is True
        assert profiling._initialized is True
        mock_pyroscope.configure.assert_called_once()

    def test_configure_called_with_correct_args(self, monkeypatch) -> None:
        """Verify pyroscope.configure() receives the expected parameters."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
        monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
        monkeypatch.setenv("PYROSCOPE_APPLICATION_NAME", "test.app")
        monkeypatch.setenv("PYROSCOPE_SAMPLE_RATE", "50")
        monkeypatch.setenv("NEXUS_ENV", "staging")

        mock_pyroscope = MagicMock()
        with (
            patch.dict("sys.modules", {"pyroscope": mock_pyroscope}),
            patch.object(profiling, "_register_otel_processor"),
        ):
            profiling.setup_profiling()

        call_kwargs = mock_pyroscope.configure.call_args[1]
        assert call_kwargs["application_name"] == "test.app"
        assert call_kwargs["server_address"] == "http://pyroscope:4040"
        assert call_kwargs["sample_rate"] == 50
        assert call_kwargs["oncpu"] is True
        assert call_kwargs["gil_only"] is True
        assert call_kwargs["detect_subprocesses"] is False

    def test_static_tags_include_env(self, monkeypatch) -> None:
        """Tags should include env and service."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
        monkeypatch.setenv("NEXUS_ENV", "production")

        mock_pyroscope = MagicMock()
        with (
            patch.dict("sys.modules", {"pyroscope": mock_pyroscope}),
            patch.object(profiling, "_register_otel_processor"),
        ):
            profiling.setup_profiling()

        tags = mock_pyroscope.configure.call_args[1]["tags"]
        assert tags["env"] == "production"
        assert tags["service"] == "nexus"

    def test_custom_tags_merged(self, monkeypatch) -> None:
        """Custom tags should be merged with defaults."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")

        mock_pyroscope = MagicMock()
        with (
            patch.dict("sys.modules", {"pyroscope": mock_pyroscope}),
            patch.object(profiling, "_register_otel_processor"),
        ):
            profiling.setup_profiling(tags={"region": "us-west-1"})

        tags = mock_pyroscope.configure.call_args[1]["tags"]
        assert tags["region"] == "us-west-1"
        assert tags["service"] == "nexus"

    def test_double_init_returns_false(self, monkeypatch) -> None:
        """Second call to setup_profiling should return False."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")

        mock_pyroscope = MagicMock()
        with (
            patch.dict("sys.modules", {"pyroscope": mock_pyroscope}),
            patch.object(profiling, "_register_otel_processor"),
        ):
            assert profiling.setup_profiling() is True
            assert profiling.setup_profiling() is False

    def test_parameter_overrides_env(self, monkeypatch) -> None:
        """Function parameters should override env vars."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
        monkeypatch.setenv("PYROSCOPE_APPLICATION_NAME", "env.app")

        mock_pyroscope = MagicMock()
        with (
            patch.dict("sys.modules", {"pyroscope": mock_pyroscope}),
            patch.object(profiling, "_register_otel_processor"),
        ):
            profiling.setup_profiling(
                application_name="override.app",
                server_address="http://custom:9999",
                sample_rate=200,
            )

        call_kwargs = mock_pyroscope.configure.call_args[1]
        assert call_kwargs["application_name"] == "override.app"
        assert call_kwargs["server_address"] == "http://custom:9999"
        assert call_kwargs["sample_rate"] == 200


# ---------------------------------------------------------------------------
# _register_otel_processor
# ---------------------------------------------------------------------------


class TestRegisterOtelProcessor:
    def test_registers_when_otel_active(self) -> None:
        """Should register PyroscopeSpanProcessor on TracerProvider."""
        mock_provider = MagicMock()
        mock_processor_cls = MagicMock()
        mock_trace = MagicMock()
        mock_trace.get_tracer_provider.return_value = mock_provider

        mock_pyroscope_otel = MagicMock()
        mock_pyroscope_otel.PyroscopeSpanProcessor = mock_processor_cls

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry": MagicMock(trace=mock_trace),
                "opentelemetry.trace": mock_trace,
                "pyroscope": MagicMock(),
                "pyroscope.otel": mock_pyroscope_otel,
            },
        ):
            profiling._register_otel_processor()

        mock_provider.add_span_processor.assert_called_once()

    def test_skips_when_otel_missing(self) -> None:
        """Should silently skip when OTel is not installed."""
        # Force ImportError by making the module None
        with patch.dict(
            "sys.modules",
            {"opentelemetry": None, "opentelemetry.trace": None},
        ):
            profiling._register_otel_processor()  # should not raise

    def test_skips_when_pyroscope_otel_missing(self) -> None:
        """Should silently skip when pyroscope-otel is not installed."""
        mock_trace = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "opentelemetry": MagicMock(trace=mock_trace),
                "opentelemetry.trace": mock_trace,
                "pyroscope.otel": None,
            },
        ):
            profiling._register_otel_processor()  # should not raise


# ---------------------------------------------------------------------------
# shutdown_profiling
# ---------------------------------------------------------------------------


class TestShutdownProfiling:
    def test_idempotent_when_not_initialized(self) -> None:
        """shutdown_profiling should be a no-op when not initialized."""
        profiling._initialized = False
        profiling.shutdown_profiling()  # should not raise

    def test_calls_pyroscope_shutdown(self) -> None:
        """Should call pyroscope.shutdown() when initialized."""
        profiling._initialized = True

        mock_pyroscope = MagicMock()
        mock_pyroscope.shutdown = MagicMock()
        with patch.dict("sys.modules", {"pyroscope": mock_pyroscope}):
            profiling.shutdown_profiling()

        assert profiling._initialized is False
        mock_pyroscope.shutdown.assert_called_once()

    def test_resets_state_on_error(self) -> None:
        """Should reset _initialized even if shutdown raises."""
        profiling._initialized = True

        mock_pyroscope = MagicMock()
        mock_pyroscope.shutdown.side_effect = RuntimeError("boom")
        with patch.dict("sys.modules", {"pyroscope": mock_pyroscope}):
            profiling.shutdown_profiling()

        assert profiling._initialized is False

    def test_handles_missing_shutdown_method(self) -> None:
        """Should handle pyroscope without shutdown() gracefully."""
        profiling._initialized = True

        mock_pyroscope = MagicMock(spec=[])  # no shutdown attr
        with patch.dict("sys.modules", {"pyroscope": mock_pyroscope}):
            profiling.shutdown_profiling()

        assert profiling._initialized is False
