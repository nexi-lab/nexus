"""Tests for SentrySubsystem — lifecycle wrapper for Sentry SDK.

Issue #759: Sentry for Error Tracking and Performance.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.services.subsystem import Subsystem
from nexus.services.subsystems.sentry_subsystem import SentrySubsystem

# ---------------------------------------------------------------------------
# Subsystem ABC compliance (standard 3 tests for every subsystem)
# ---------------------------------------------------------------------------


class TestSentrySubsystemCompliance:
    """Subsystem ABC contract tests."""

    def test_is_subsystem_instance(self) -> None:
        sub = SentrySubsystem(enabled=False)
        assert isinstance(sub, Subsystem)

    def test_health_check_returns_dict_with_status(self) -> None:
        sub = SentrySubsystem(enabled=False)
        result = sub.health_check()
        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] in ("ok", "degraded")

    def test_cleanup_callable_and_no_raise(self) -> None:
        sub = SentrySubsystem(enabled=False)
        assert callable(sub.cleanup)
        sub.cleanup()  # Should not raise


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Health check output for enabled/disabled states."""

    def test_disabled_health_check(self) -> None:
        sub = SentrySubsystem(enabled=False)
        health = sub.health_check()
        assert health["status"] == "ok"
        assert health["subsystem"] == "sentry"
        assert health["enabled"] is False
        assert "last_event_id" not in health

    def test_enabled_health_check_with_no_events(self) -> None:
        sub = SentrySubsystem(enabled=True)
        mock_sdk = MagicMock()
        mock_sdk.last_event_id.return_value = None

        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            health = sub.health_check()

        assert health["status"] == "ok"
        assert health["subsystem"] == "sentry"
        assert health["enabled"] is True
        assert health["last_event_id"] is None

    def test_enabled_health_check_with_event_id(self) -> None:
        sub = SentrySubsystem(enabled=True)
        mock_sdk = MagicMock()
        mock_sdk.last_event_id.return_value = "abc123def456"

        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            health = sub.health_check()

        assert health["last_event_id"] == "abc123def456"

    def test_enabled_health_check_handles_import_error(self) -> None:
        """If sentry_sdk is not importable, health check still returns ok."""
        sub = SentrySubsystem(enabled=True)

        # Remove sentry_sdk from modules so import fails
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            health = sub.health_check()

        assert health["status"] == "ok"
        assert health["enabled"] is True
        assert health["last_event_id"] is None


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """Cleanup flushes pending events."""

    def test_cleanup_noop_when_disabled(self) -> None:
        sub = SentrySubsystem(enabled=False)
        # Should not raise even without sentry_sdk
        sub.cleanup()

    def test_cleanup_flushes_when_enabled(self) -> None:
        sub = SentrySubsystem(enabled=True)
        mock_sdk = MagicMock()

        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            sub.cleanup()

        mock_sdk.flush.assert_called_once_with(timeout=2.0)

    def test_cleanup_handles_flush_error(self) -> None:
        sub = SentrySubsystem(enabled=True)
        mock_sdk = MagicMock()
        mock_sdk.flush.side_effect = RuntimeError("network error")

        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            # Should not raise
            sub.cleanup()


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """Property accessor tests."""

    def test_enabled_property(self) -> None:
        assert SentrySubsystem(enabled=True).enabled is True
        assert SentrySubsystem(enabled=False).enabled is False
