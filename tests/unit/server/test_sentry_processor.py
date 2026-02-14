"""Tests for Sentry structlog processor.

Issue #759: Sentry for Error Tracking and Performance.

Tests cover:
- No-op behavior when structlog-sentry is not installed
- Expected errors (should_alert=False) skip Sentry
- Unexpected errors (should_alert=True or absent) delegate to SentryProcessor
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# No-op when unavailable
# ---------------------------------------------------------------------------


class TestNoopProcessor:
    """Tests for graceful degradation when structlog-sentry is not installed."""

    def test_returns_noop_when_sentry_not_enabled(self) -> None:
        """When SENTRY_DSN is not set, create_sentry_processor returns noop."""
        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("SENTRY_DSN", None)

            # Reset module-level cache
            import nexus.server.sentry_processor as mod

            original_has = mod._HAS_STRUCTLOG_SENTRY
            mod._HAS_STRUCTLOG_SENTRY = True  # pretend it's installed

            try:
                processor = mod.create_sentry_processor()
                # Should be noop since DSN is not set
                event_dict = {"event": "test", "level": "error"}
                result = processor(None, None, event_dict)
                assert result == event_dict
            finally:
                mod._HAS_STRUCTLOG_SENTRY = original_has

    def test_returns_noop_when_structlog_sentry_missing(self) -> None:
        """When structlog-sentry is not installed, returns identity function."""
        import nexus.server.sentry_processor as mod

        original_has = mod._HAS_STRUCTLOG_SENTRY
        mod._HAS_STRUCTLOG_SENTRY = False

        try:
            processor = mod.create_sentry_processor()
            event_dict = {"event": "error happened", "level": "error"}
            result = processor(None, None, event_dict)
            assert result is event_dict
        finally:
            mod._HAS_STRUCTLOG_SENTRY = original_has

    def test_noop_processor_is_identity(self) -> None:
        """_noop_processor passes event_dict through unchanged."""
        from nexus.server.sentry_processor import _noop_processor

        event_dict = {"event": "test", "extra_key": "value"}
        result = _noop_processor(None, None, event_dict)
        assert result is event_dict


# ---------------------------------------------------------------------------
# Filtering behavior
# ---------------------------------------------------------------------------


class TestSentryFilteringProcessor:
    """Tests for the should_alert filtering logic."""

    def test_skips_expected_errors(self) -> None:
        """When should_alert=False, event is passed through without Sentry."""
        # Mock the real processor to track calls
        import nexus.server.sentry_processor as mod
        from nexus.server.sentry_processor import _sentry_filtering_processor

        original = mod._real_processor
        mock_processor = MagicMock(return_value={"event": "processed"})
        mod._real_processor = mock_processor

        try:
            event_dict = {
                "event": "expected error",
                "level": "error",
                "should_alert": False,
            }
            result = _sentry_filtering_processor(None, None, event_dict)
            # Should NOT delegate to real processor
            mock_processor.assert_not_called()
            assert result is event_dict
        finally:
            mod._real_processor = original

    def test_delegates_unexpected_errors(self) -> None:
        """When should_alert=True, delegates to real SentryProcessor."""
        import nexus.server.sentry_processor as mod
        from nexus.server.sentry_processor import _sentry_filtering_processor

        original = mod._real_processor
        mock_processor = MagicMock(return_value={"event": "sent to sentry"})
        mod._real_processor = mock_processor

        try:
            event_dict = {
                "event": "unexpected error",
                "level": "error",
                "should_alert": True,
            }
            result = _sentry_filtering_processor(None, None, event_dict)
            mock_processor.assert_called_once()
            assert result == {"event": "sent to sentry"}
        finally:
            mod._real_processor = original

    def test_delegates_when_should_alert_absent(self) -> None:
        """When should_alert is not in event_dict, delegates to Sentry."""
        import nexus.server.sentry_processor as mod
        from nexus.server.sentry_processor import _sentry_filtering_processor

        original = mod._real_processor
        mock_processor = MagicMock(return_value={"event": "sent"})
        mod._real_processor = mock_processor

        try:
            event_dict = {"event": "some error", "level": "error"}
            _sentry_filtering_processor(None, None, event_dict)
            mock_processor.assert_called_once()
        finally:
            mod._real_processor = original

    def test_non_error_events_pass_through_when_should_alert_false(self) -> None:
        """Info/warning events with should_alert=False are not sent to Sentry."""
        import nexus.server.sentry_processor as mod
        from nexus.server.sentry_processor import _sentry_filtering_processor

        original = mod._real_processor
        mock_processor = MagicMock()
        mod._real_processor = mock_processor

        try:
            event_dict = {"event": "info msg", "level": "info", "should_alert": False}
            result = _sentry_filtering_processor(None, None, event_dict)
            mock_processor.assert_not_called()
            assert result is event_dict
        finally:
            mod._real_processor = original
