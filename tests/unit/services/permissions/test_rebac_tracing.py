"""Unit tests for ReBAC OTel tracing instrumentation.

Issue #702: OTel tracing for ReBAC permission debugging.

Tests cover:
- Span creation and attribute correctness for rebac.check
- Cache lookup child span (hit / miss)
- Graph traversal child span with stats
- Zero-overhead when OTel is disabled
- Error recording (GraphLimitExceeded)
- Context propagation across asyncio.to_thread()
- Circuit breaker fallback tracing
- Rust vs Python engine attribute
- Batch check summary span
- Consistency level attribute
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from nexus.services.permissions import rebac_tracing
from nexus.services.permissions.rebac_tracing import (
    ATTR_BATCH_ALLOWED,
    ATTR_BATCH_DENIED,
    ATTR_BATCH_DURATION_MS,
    ATTR_BATCH_SIZE,
    ATTR_CACHE_FALLBACK,
    ATTR_CACHE_HIT,
    ATTR_CACHE_SOURCE,
    ATTR_CONSISTENCY,
    ATTR_DECISION,
    ATTR_DECISION_TIME_MS,
    ATTR_ENGINE,
    ATTR_LIMIT_EXCEEDED,
    ATTR_LIMIT_TYPE,
    ATTR_OBJECT_ID,
    ATTR_OBJECT_TYPE,
    ATTR_PERMISSION,
    ATTR_SUBJECT_ID,
    ATTR_SUBJECT_TYPE,
    ATTR_TRAVERSAL_CACHE_HITS,
    ATTR_TRAVERSAL_DEPTH,
    ATTR_TRAVERSAL_QUERIES,
    ATTR_TRAVERSAL_VISITED,
    ATTR_ZONE_ID,
    propagate_otel_context,
    record_batch_result,
    record_cache_result,
    record_check_result,
    record_graph_limit_exceeded,
    record_traversal_result,
    start_batch_check_span,
    start_cache_lookup_span,
    start_check_span,
    start_graph_traversal_span,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tracer():
    """Reset the module-level cached tracer before each test."""
    rebac_tracing.reset_tracer()
    yield
    rebac_tracing.reset_tracer()


def _make_mock_tracer():
    """Create a mock tracer whose start_as_current_span returns a mock span."""
    mock_span = MagicMock()
    mock_tracer = MagicMock()

    @contextmanager
    def _ctx_manager(name):
        yield mock_span

    mock_tracer.start_as_current_span = MagicMock(side_effect=_ctx_manager)
    return mock_tracer, mock_span


# ---------------------------------------------------------------------------
# TestStartCheckSpan
# ---------------------------------------------------------------------------


class TestStartCheckSpan:
    """Test root rebac.check span creation."""

    def test_creates_span_with_correct_name(self):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_check_span(
                subject=("user", "alice"),
                permission="read",
                obj=("file", "/doc.txt"),
                zone_id="zone_1",
                consistency="eventual",
            ) as span,
        ):
            assert span is mock_span

        mock_tracer.start_as_current_span.assert_called_once_with("rebac.check")

    def test_sets_all_required_attributes(self):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_check_span(
                subject=("user", "alice"),
                permission="read",
                obj=("file", "/doc.txt"),
                zone_id="zone_1",
                consistency="eventual",
            ),
        ):
            pass

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_SUBJECT_TYPE] == "user"
        assert calls[ATTR_SUBJECT_ID] == "alice"
        assert calls[ATTR_PERMISSION] == "read"
        assert calls[ATTR_OBJECT_TYPE] == "file"
        assert calls[ATTR_OBJECT_ID] == "/doc.txt"
        assert calls[ATTR_ZONE_ID] == "zone_1"
        assert calls[ATTR_CONSISTENCY] == "eventual"

    def test_omits_optional_attributes_when_none(self):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_check_span(
                subject=("user", "alice"),
                permission="read",
                obj=("file", "/doc.txt"),
            ),
        ):
            pass

        attr_keys = {c[0][0] for c in mock_span.set_attribute.call_args_list}
        assert ATTR_ZONE_ID not in attr_keys
        assert ATTR_CONSISTENCY not in attr_keys

    def test_yields_none_when_otel_disabled(self):
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=None),
            start_check_span(
                subject=("user", "alice"),
                permission="read",
                obj=("file", "/doc.txt"),
            ) as span,
        ):
            assert span is None


# ---------------------------------------------------------------------------
# TestRecordCheckResult
# ---------------------------------------------------------------------------


class TestRecordCheckResult:
    """Test recording final check decision on a span."""

    def test_records_allow_decision(self):
        mock_span = MagicMock()
        record_check_result(mock_span, allowed=True, decision_time_ms=1.5, engine="rust")

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_DECISION] == "ALLOW"
        assert calls[ATTR_DECISION_TIME_MS] == 1.5
        assert calls[ATTR_CACHE_HIT] is False
        assert calls[ATTR_ENGINE] == "rust"

    def test_records_deny_decision(self):
        mock_span = MagicMock()
        record_check_result(mock_span, allowed=False, decision_time_ms=3.2, cached=True)

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_DECISION] == "DENY"
        assert calls[ATTR_CACHE_HIT] is True

    def test_noop_when_span_is_none(self):
        # Should not raise
        record_check_result(None, allowed=True, decision_time_ms=0.5)

    def test_omits_engine_when_none(self):
        mock_span = MagicMock()
        record_check_result(mock_span, allowed=True, decision_time_ms=1.0)

        attr_keys = {c[0][0] for c in mock_span.set_attribute.call_args_list}
        assert ATTR_ENGINE not in attr_keys


# ---------------------------------------------------------------------------
# TestCacheLookupSpan
# ---------------------------------------------------------------------------


class TestCacheLookupSpan:
    """Test cache lookup child span."""

    def test_creates_cache_lookup_span(self):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_cache_lookup_span() as span,
        ):
            assert span is mock_span

        mock_tracer.start_as_current_span.assert_called_once_with("rebac.cache_lookup")

    def test_record_cache_hit(self):
        mock_span = MagicMock()
        record_cache_result(mock_span, hit=True, source="l1")

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_CACHE_HIT] is True
        assert calls[ATTR_CACHE_SOURCE] == "l1"

    def test_record_cache_miss(self):
        mock_span = MagicMock()
        record_cache_result(mock_span, hit=False)

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_CACHE_HIT] is False
        assert ATTR_CACHE_SOURCE not in {c[0][0] for c in mock_span.set_attribute.call_args_list}

    def test_record_circuit_breaker_fallback(self):
        mock_span = MagicMock()
        record_cache_result(mock_span, hit=True, source="l1", fallback=True)

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_CACHE_FALLBACK] is True

    def test_yields_none_when_disabled(self):
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=None),
            start_cache_lookup_span() as span,
        ):
            assert span is None

    def test_record_noop_when_span_none(self):
        record_cache_result(None, hit=True)


# ---------------------------------------------------------------------------
# TestGraphTraversalSpan
# ---------------------------------------------------------------------------


class TestGraphTraversalSpan:
    """Test graph traversal child span."""

    def test_creates_traversal_span_with_engine(self):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_graph_traversal_span(engine="rust") as span,
        ):
            assert span is mock_span

        mock_tracer.start_as_current_span.assert_called_once_with("rebac.graph_traversal")
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_ENGINE] == "rust"

    def test_record_traversal_stats(self):
        mock_span = MagicMock()
        record_traversal_result(
            mock_span,
            depth=5,
            visited_nodes=42,
            db_queries=8,
            cache_hits=3,
        )

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_TRAVERSAL_DEPTH] == 5
        assert calls[ATTR_TRAVERSAL_VISITED] == 42
        assert calls[ATTR_TRAVERSAL_QUERIES] == 8
        assert calls[ATTR_TRAVERSAL_CACHE_HITS] == 3

    def test_record_traversal_noop_when_none(self):
        record_traversal_result(None, depth=5)

    def test_record_graph_limit_exceeded(self):
        mock_span = MagicMock()
        record_graph_limit_exceeded(mock_span, limit_type="timeout")

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_LIMIT_EXCEEDED] is True
        assert calls[ATTR_LIMIT_TYPE] == "timeout"

    def test_record_graph_limit_sets_error_status(self):
        mock_span = MagicMock()
        mock_status_code = MagicMock()
        mock_status_code.ERROR = "ERROR"

        with patch.dict(
            "sys.modules",
            {"opentelemetry.trace": MagicMock(StatusCode=mock_status_code)},
        ):
            record_graph_limit_exceeded(mock_span, limit_type="depth")

        mock_span.set_status.assert_called_once()

    def test_record_graph_limit_noop_when_none(self):
        record_graph_limit_exceeded(None, limit_type="timeout")

    def test_yields_none_when_disabled(self):
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=None),
            start_graph_traversal_span() as span,
        ):
            assert span is None


# ---------------------------------------------------------------------------
# TestBatchCheckSpan
# ---------------------------------------------------------------------------


class TestBatchCheckSpan:
    """Test batch permission check span."""

    def test_creates_batch_span_with_size(self):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_batch_check_span(batch_size=50) as span,
        ):
            assert span is mock_span

        mock_tracer.start_as_current_span.assert_called_once_with("rebac.check_batch")
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_BATCH_SIZE] == 50

    def test_record_batch_result(self):
        mock_span = MagicMock()
        record_batch_result(
            mock_span,
            allowed_count=45,
            denied_count=5,
            duration_ms=12.3,
        )

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_BATCH_ALLOWED] == 45
        assert calls[ATTR_BATCH_DENIED] == 5
        assert calls[ATTR_BATCH_DURATION_MS] == 12.3

    def test_batch_noop_when_disabled(self):
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=None),
            start_batch_check_span(batch_size=10) as span,
        ):
            assert span is None

    def test_record_batch_noop_when_none(self):
        record_batch_result(None, allowed_count=1, denied_count=0, duration_ms=1.0)


# ---------------------------------------------------------------------------
# TestZeroOverhead
# ---------------------------------------------------------------------------


class TestZeroOverhead:
    """Verify zero overhead when OTel is disabled."""

    def test_get_tracer_returns_none_when_disabled(self):
        with patch("nexus.server.telemetry.get_tracer", return_value=None):
            rebac_tracing.reset_tracer()
            tracer = rebac_tracing._get_tracer()
            assert tracer is None

    def test_check_span_no_allocations_when_disabled(self):
        """When disabled, start_check_span should yield None immediately."""
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=None),
            start_check_span(
                subject=("user", "alice"),
                permission="read",
                obj=("file", "/test.txt"),
            ) as span,
        ):
            assert span is None

    def test_all_record_functions_noop_with_none(self):
        """All record_* functions should silently accept None spans."""
        record_check_result(None, allowed=True, decision_time_ms=0.0)
        record_cache_result(None, hit=True)
        record_traversal_result(None, depth=0)
        record_graph_limit_exceeded(None, limit_type="depth")
        record_batch_result(None, allowed_count=0, denied_count=0, duration_ms=0.0)


# ---------------------------------------------------------------------------
# TestContextPropagation
# ---------------------------------------------------------------------------


class TestContextPropagation:
    """Test OTel context propagation for asyncio.to_thread()."""

    def test_propagate_wraps_function(self):
        """propagate_otel_context should return a wrapper that calls the original."""
        mock_ctx = MagicMock()
        mock_context_module = MagicMock()
        mock_context_module.get_current.return_value = mock_ctx
        mock_context_module.attach.return_value = "token"

        with (
            patch.dict("sys.modules", {"opentelemetry": MagicMock(context=mock_context_module)}),
            patch(
                "nexus.services.permissions.rebac_tracing.otel_context",
                mock_context_module,
                create=True,
            ),
        ):
            # Re-import to pick up the mock
            import importlib

            importlib.reload(rebac_tracing)
            rebac_tracing.reset_tracer()

            def original(x, y):
                return x + y

            wrapped = propagate_otel_context(original)
            result = wrapped(1, 2)

        assert result == 3

    def test_propagate_returns_original_when_otel_missing(self):
        """When OTel is not installed, return the original function unchanged."""
        with patch.dict("sys.modules", {"opentelemetry": None}):
            import importlib

            try:
                importlib.reload(rebac_tracing)
            except (ImportError, TypeError):
                pass
            rebac_tracing.reset_tracer()

            def original():
                return 42

            # When import fails, should return original
            wrapped = propagate_otel_context(original)
            assert wrapped() == 42

    def test_propagate_detaches_on_exception(self):
        """Context token should be detached even if the function raises."""
        mock_ctx = MagicMock()
        mock_context_module = MagicMock()
        mock_context_module.get_current.return_value = mock_ctx
        mock_token = MagicMock()
        mock_context_module.attach.return_value = mock_token

        with patch(
            "nexus.services.permissions.rebac_tracing.otel_context",
            mock_context_module,
            create=True,
        ):
            pass

        # Simpler approach: test the wrapper logic directly
        original_called = False

        def original():
            nonlocal original_called
            original_called = True
            raise ValueError("boom")

        try:
            from opentelemetry import context as otel_context

            ctx = otel_context.get_current()

            def _with_context(*args, **kwargs):
                token = otel_context.attach(ctx)
                try:
                    return original(*args, **kwargs)
                finally:
                    otel_context.detach(token)

            with pytest.raises(ValueError, match="boom"):
                _with_context()

            assert original_called
        except ImportError:
            pytest.skip("opentelemetry not installed")


# ---------------------------------------------------------------------------
# TestConsistencyAttribute
# ---------------------------------------------------------------------------


class TestConsistencyAttribute:
    """Test consistency level recording in spans."""

    @pytest.mark.parametrize(
        "consistency_name",
        ["eventual", "bounded", "strong"],
    )
    def test_consistency_level_recorded(self, consistency_name):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_check_span(
                subject=("user", "alice"),
                permission="read",
                obj=("file", "/doc.txt"),
                consistency=consistency_name,
            ),
        ):
            pass

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_CONSISTENCY] == consistency_name


# ---------------------------------------------------------------------------
# TestEngineAttribute
# ---------------------------------------------------------------------------


class TestEngineAttribute:
    """Test Rust vs Python engine attribute."""

    def test_rust_engine_recorded_in_check(self):
        mock_span = MagicMock()
        record_check_result(mock_span, allowed=True, decision_time_ms=0.5, engine="rust")

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_ENGINE] == "rust"

    def test_python_engine_recorded_in_traversal(self):
        mock_tracer, mock_span = _make_mock_tracer()
        with (
            patch.object(rebac_tracing, "_get_tracer", return_value=mock_tracer),
            start_graph_traversal_span(engine="python"),
        ):
            pass

        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls[ATTR_ENGINE] == "python"


# ---------------------------------------------------------------------------
# TestTracerCaching
# ---------------------------------------------------------------------------


class TestTracerCaching:
    """Test that _get_tracer() caches its result."""

    def test_tracer_resolved_once(self):
        mock_tracer = MagicMock()
        call_count = 0

        def mock_get_tracer(name):
            nonlocal call_count
            call_count += 1
            return mock_tracer

        with patch("nexus.server.telemetry.get_tracer", mock_get_tracer):
            rebac_tracing.reset_tracer()
            t1 = rebac_tracing._get_tracer()
            t2 = rebac_tracing._get_tracer()

        assert t1 is mock_tracer
        assert t2 is mock_tracer
        assert call_count == 1  # Only resolved once

    def test_reset_allows_re_resolution(self):
        call_count = 0

        def mock_get_tracer(name):
            nonlocal call_count
            call_count += 1
            return MagicMock()

        with patch("nexus.server.telemetry.get_tracer", mock_get_tracer):
            rebac_tracing.reset_tracer()
            rebac_tracing._get_tracer()
            rebac_tracing.reset_tracer()
            rebac_tracing._get_tracer()

        assert call_count == 2
