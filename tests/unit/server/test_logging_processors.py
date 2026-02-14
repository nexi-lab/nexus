"""Tests for structlog processors.

Issue #1002: Structured JSON logging with request correlation.

Tests custom processors: OTel trace bridge, error classification, service name.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.server.logging_processors import (
    add_service_name,
    error_classification_processor,
    otel_trace_processor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event_dict(**kwargs: object) -> dict:
    """Create a minimal structlog event dict for processor testing."""
    base: dict = {"event": "test event", "_record": MagicMock()}
    base.update(kwargs)
    return base


def _make_mock_span(
    trace_id: int = 0x1234567890ABCDEF1234567890ABCDEF,
    span_id: int = 0x1234567890ABCDEF,
    recording: bool = True,
) -> MagicMock:
    """Create a mock OTel span with configurable context."""
    span = MagicMock()
    ctx = MagicMock()
    ctx.trace_id = trace_id
    ctx.span_id = span_id
    span.get_span_context.return_value = ctx
    span.is_recording.return_value = recording
    return span


# ---------------------------------------------------------------------------
# OTel trace processor tests
# ---------------------------------------------------------------------------


class TestOtelTraceProcessor:
    """Injects trace_id/span_id from OTel active span."""

    def test_adds_trace_id_when_span_active(self) -> None:
        mock_span = _make_mock_span()

        with (
            patch("nexus.server.logging_processors._HAS_OTEL", True),
            patch("nexus.server.logging_processors._otel_trace") as mock_trace,
        ):
            mock_trace.get_current_span.return_value = mock_span
            event_dict = otel_trace_processor(None, None, _make_event_dict())

        assert "trace_id" in event_dict
        assert isinstance(event_dict["trace_id"], str)
        assert len(event_dict["trace_id"]) == 32  # 128-bit as 32 hex chars

    def test_adds_span_id_when_span_active(self) -> None:
        mock_span = _make_mock_span()

        with (
            patch("nexus.server.logging_processors._HAS_OTEL", True),
            patch("nexus.server.logging_processors._otel_trace") as mock_trace,
        ):
            mock_trace.get_current_span.return_value = mock_span
            event_dict = otel_trace_processor(None, None, _make_event_dict())

        assert "span_id" in event_dict
        assert isinstance(event_dict["span_id"], str)
        assert len(event_dict["span_id"]) == 16  # 64-bit as 16 hex chars

    def test_noop_when_otel_not_available(self) -> None:
        with patch("nexus.server.logging_processors._HAS_OTEL", False):
            event_dict = otel_trace_processor(None, None, _make_event_dict())

        assert "trace_id" not in event_dict
        assert "span_id" not in event_dict

    def test_noop_when_no_active_span(self) -> None:
        with (
            patch("nexus.server.logging_processors._HAS_OTEL", True),
            patch("nexus.server.logging_processors._otel_trace") as mock_trace,
        ):
            mock_trace.get_current_span.return_value = None
            event_dict = otel_trace_processor(None, None, _make_event_dict())

        assert "trace_id" not in event_dict
        assert "span_id" not in event_dict

    def test_noop_when_span_not_recording(self) -> None:
        mock_span = _make_mock_span(recording=False)

        with (
            patch("nexus.server.logging_processors._HAS_OTEL", True),
            patch("nexus.server.logging_processors._otel_trace") as mock_trace,
        ):
            mock_trace.get_current_span.return_value = mock_span
            event_dict = otel_trace_processor(None, None, _make_event_dict())

        assert "trace_id" not in event_dict
        assert "span_id" not in event_dict

    def test_formats_trace_id_as_lowercase_hex(self) -> None:
        mock_span = _make_mock_span(
            trace_id=0xABCDEF1234567890ABCDEF1234567890,
            span_id=0xABCDEF1234567890,
        )

        with (
            patch("nexus.server.logging_processors._HAS_OTEL", True),
            patch("nexus.server.logging_processors._otel_trace") as mock_trace,
        ):
            mock_trace.get_current_span.return_value = mock_span
            event_dict = otel_trace_processor(None, None, _make_event_dict())

        assert event_dict["trace_id"] == event_dict["trace_id"].lower()

    def test_exception_in_otel_does_not_crash(self) -> None:
        """OTel errors should never break logging."""
        with (
            patch("nexus.server.logging_processors._HAS_OTEL", True),
            patch("nexus.server.logging_processors._otel_trace") as mock_trace,
        ):
            mock_trace.get_current_span.side_effect = RuntimeError("otel broke")
            event_dict = otel_trace_processor(None, None, _make_event_dict())

        # Should return event_dict unchanged, not raise
        assert event_dict["event"] == "test event"
        assert "trace_id" not in event_dict


# ---------------------------------------------------------------------------
# Error classification processor tests
# ---------------------------------------------------------------------------


class TestErrorClassificationProcessor:
    """Classifies errors as expected/unexpected and adjusts log metadata."""

    def test_expected_error_marked(self) -> None:
        exc = Exception("not found")
        exc.is_expected = True  # type: ignore[attr-defined]

        event_dict = _make_event_dict(exc_info=(type(exc), exc, None))
        result = error_classification_processor(None, None, event_dict)

        assert result["error_expected"] is True

    def test_unexpected_error_marked(self) -> None:
        exc = Exception("internal failure")
        # No is_expected attribute -> unexpected

        event_dict = _make_event_dict(exc_info=(type(exc), exc, None))
        result = error_classification_processor(None, None, event_dict)

        assert result["error_expected"] is False

    def test_adds_should_alert_field(self) -> None:
        exc = Exception("db connection lost")
        event_dict = _make_event_dict(exc_info=(type(exc), exc, None))
        result = error_classification_processor(None, None, event_dict)

        assert result["should_alert"] is True

    def test_expected_error_no_alert(self) -> None:
        exc = Exception("file not found")
        exc.is_expected = True  # type: ignore[attr-defined]

        event_dict = _make_event_dict(exc_info=(type(exc), exc, None))
        result = error_classification_processor(None, None, event_dict)

        assert result["should_alert"] is False

    def test_no_exception_passthrough(self) -> None:
        event_dict = _make_event_dict(some_field="value")
        result = error_classification_processor(None, None, event_dict)

        assert "error_expected" not in result
        assert "should_alert" not in result
        assert result["some_field"] == "value"

    def test_exc_info_false_passthrough(self) -> None:
        event_dict = _make_event_dict(exc_info=False)
        result = error_classification_processor(None, None, event_dict)

        assert "error_expected" not in result

    def test_exc_info_true_resolves_active_exception(self) -> None:
        """exc_info=True should resolve to current exception via sys.exc_info()."""
        exc = ValueError("resolved from sys")
        exc.is_expected = True  # type: ignore[attr-defined]

        event_dict = _make_event_dict(exc_info=True)

        # Simulate being inside an except block
        try:
            raise exc
        except ValueError:
            result = error_classification_processor(None, None, event_dict)

        assert result["error_expected"] is True
        assert result["should_alert"] is False

    def test_exc_info_true_no_active_exception_passthrough(self) -> None:
        """exc_info=True with no active exception should be a no-op."""
        event_dict = _make_event_dict(exc_info=True)
        result = error_classification_processor(None, None, event_dict)

        assert "error_expected" not in result
        assert "should_alert" not in result

    def test_bare_exception_instance(self) -> None:
        """structlog convention: passing exception directly as exc_info."""
        exc = RuntimeError("direct exception")
        event_dict = _make_event_dict(exc_info=exc)
        result = error_classification_processor(None, None, event_dict)

        assert result["error_expected"] is False
        assert result["should_alert"] is True


# ---------------------------------------------------------------------------
# Service name processor tests
# ---------------------------------------------------------------------------


class TestAddServiceName:
    """Adds service name to every log event."""

    def test_adds_default_service_name(self) -> None:
        event_dict = _make_event_dict()
        result = add_service_name(None, None, event_dict)

        assert result["service"] == "nexus"

    def test_does_not_overwrite_existing_service(self) -> None:
        event_dict = _make_event_dict(service="custom-service")
        result = add_service_name(None, None, event_dict)

        assert result["service"] == "custom-service"

    def test_reads_from_env_var(self) -> None:
        with patch("nexus.server.logging_processors._SERVICE_NAME", "nexus-worker"):
            event_dict = _make_event_dict()
            result = add_service_name(None, None, event_dict)

        assert result["service"] == "nexus-worker"


# ---------------------------------------------------------------------------
# Integration test: full pipeline
# ---------------------------------------------------------------------------


class TestProcessorPipelineIntegration:
    """All processors compose correctly in a pipeline."""

    def test_pipeline_produces_valid_output(self) -> None:
        event_dict = _make_event_dict(user_id="u42")

        # Run through all processors sequentially
        with patch("nexus.server.logging_processors._HAS_OTEL", False):
            result = otel_trace_processor(None, None, event_dict)
        result = error_classification_processor(None, None, result)
        result = add_service_name(None, None, result)

        # Core fields present
        assert result["event"] == "test event"
        assert result["service"] == "nexus"
        assert result["user_id"] == "u42"
        # No OTel fields (no active span)
        assert "trace_id" not in result
        # No error fields (no exception)
        assert "error_expected" not in result
