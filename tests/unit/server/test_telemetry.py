"""Tests for OpenTelemetry instrumentation module.

Issue #1002: Structured JSON logging with request correlation.
Pre-existing gap: server/telemetry.py had 0% test coverage.

Tests cover setup, shutdown, tracer access, span operations,
environment variable parsing, and graceful degradation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.server import telemetry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_telemetry_state():
    """Reset telemetry module state before each test."""
    telemetry._initialized = False
    telemetry._tracer = None
    yield
    telemetry._initialized = False
    telemetry._tracer = None


@pytest.fixture
def _clean_env(monkeypatch):
    """Remove all OTEL_ env vars for a clean test."""
    for var in (
        "OTEL_ENABLED",
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_INSECURE",
        "OTEL_TRACES_SAMPLER_ARG",
        "OTEL_ENVIRONMENT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# is_telemetry_enabled
# ---------------------------------------------------------------------------


class TestIsTelemetryEnabled:
    def test_enabled_true(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        assert telemetry.is_telemetry_enabled() is True

    def test_enabled_1(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "1")
        assert telemetry.is_telemetry_enabled() is True

    def test_enabled_yes(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "yes")
        assert telemetry.is_telemetry_enabled() is True

    def test_disabled_false(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "false")
        assert telemetry.is_telemetry_enabled() is False

    def test_disabled_default(self, _clean_env) -> None:
        assert telemetry.is_telemetry_enabled() is False

    def test_disabled_random_string(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "maybe")
        assert telemetry.is_telemetry_enabled() is False


# ---------------------------------------------------------------------------
# setup_telemetry
# ---------------------------------------------------------------------------


class TestSetupTelemetry:
    def test_returns_false_when_disabled(self, _clean_env) -> None:
        result = telemetry.setup_telemetry()
        assert result is False

    def test_returns_false_when_already_initialized(self, monkeypatch) -> None:
        telemetry._initialized = True
        monkeypatch.setenv("OTEL_ENABLED", "true")
        result = telemetry.setup_telemetry()
        assert result is False

    def test_returns_true_when_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")

        # Mock OTel imports to avoid real exporter connection
        mock_provider = MagicMock()
        mock_trace = MagicMock()
        mock_trace.get_tracer.return_value = MagicMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "opentelemetry": MagicMock(trace=mock_trace),
                    "opentelemetry.trace": mock_trace,
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(),
                    "opentelemetry.sdk.resources": MagicMock(),
                    "opentelemetry.sdk.trace": MagicMock(
                        TracerProvider=MagicMock(return_value=mock_provider)
                    ),
                    "opentelemetry.sdk.trace.export": MagicMock(),
                    "opentelemetry.sdk.trace.sampling": MagicMock(),
                },
            ),
            patch.object(telemetry, "_instrument_libraries"),
        ):
            # Need to reload to pick up mocked imports
            result = telemetry.setup_telemetry()

        assert result is True
        assert telemetry._initialized is True

    def test_returns_false_when_otel_not_installed(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")

        # Force ImportError on OTel imports
        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def mock_import(name, *args, **kwargs):
            if name.startswith("opentelemetry"):
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            # Reset to force re-import
            telemetry._initialized = False
            result = telemetry.setup_telemetry()

        assert result is False


# ---------------------------------------------------------------------------
# get_tracer
# ---------------------------------------------------------------------------


class TestGetTracer:
    def test_returns_none_before_init(self) -> None:
        assert telemetry._initialized is False
        result = telemetry.get_tracer("test")
        assert result is None

    def test_returns_tracer_after_init(self) -> None:
        telemetry._initialized = True
        mock_tracer = MagicMock()

        with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
            result = telemetry.get_tracer("test.module")

        assert result is mock_tracer


# ---------------------------------------------------------------------------
# instrument_fastapi_app
# ---------------------------------------------------------------------------


class TestInstrumentFastapiApp:
    def test_returns_false_when_disabled(self) -> None:
        app = MagicMock()
        result = telemetry.instrument_fastapi_app(app)
        assert result is False

    def test_returns_false_when_not_initialized(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        telemetry._initialized = False
        app = MagicMock()
        result = telemetry.instrument_fastapi_app(app)
        assert result is False

    def test_returns_true_when_ready(self, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        telemetry._initialized = True
        app = MagicMock()

        mock_instrumentor = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "opentelemetry.instrumentation.fastapi": MagicMock(
                    FastAPIInstrumentor=mock_instrumentor
                ),
            },
        ):
            result = telemetry.instrument_fastapi_app(app)

        assert result is True


# ---------------------------------------------------------------------------
# add_span_attribute
# ---------------------------------------------------------------------------


class TestAddSpanAttribute:
    def test_noop_when_not_initialized(self) -> None:
        # Should not raise
        telemetry.add_span_attribute("key", "value")

    def test_sets_attribute_when_initialized(self) -> None:
        telemetry._initialized = True
        mock_span = MagicMock()

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            telemetry.add_span_attribute("test.key", "test_value")

        mock_span.set_attribute.assert_called_once_with("test.key", "test_value")


# ---------------------------------------------------------------------------
# record_exception
# ---------------------------------------------------------------------------


class TestRecordException:
    def test_noop_when_not_initialized(self) -> None:
        telemetry.record_exception(ValueError("test"))

    def test_records_exception_when_initialized(self) -> None:
        telemetry._initialized = True
        mock_span = MagicMock()
        exc = ValueError("test error")

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            telemetry.record_exception(exc)

        mock_span.record_exception.assert_called_once_with(exc)


# ---------------------------------------------------------------------------
# shutdown_telemetry
# ---------------------------------------------------------------------------


class TestShutdownTelemetry:
    def test_noop_when_not_initialized(self) -> None:
        telemetry.shutdown_telemetry()
        assert telemetry._initialized is False

    def test_clears_state(self) -> None:
        telemetry._initialized = True
        telemetry._tracer = MagicMock()

        mock_provider = MagicMock()
        with patch("opentelemetry.trace.get_tracer_provider", return_value=mock_provider):
            telemetry.shutdown_telemetry()

        assert telemetry._initialized is False
        assert telemetry._tracer is None
        mock_provider.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# _get_version
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_returns_version_string(self) -> None:
        result = telemetry._get_version()
        assert isinstance(result, str)

    def test_returns_unknown_on_error(self) -> None:
        with patch("importlib.metadata.version", side_effect=Exception("not installed")):
            result = telemetry._get_version()
        assert result == "unknown"
