"""Sentry Subsystem — lifecycle wrapper for Sentry SDK health monitoring.

Issue #759: Sentry for Error Tracking and Performance.

Provides ``SentrySubsystem(Subsystem)`` with ``health_check()`` and ``cleanup()``.
Registered in the factory alongside ObservabilitySubsystem.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.services.subsystem import Subsystem

logger = logging.getLogger(__name__)


class SentrySubsystem(Subsystem):
    """Lifecycle wrapper for Sentry SDK.

    Reports enabled/disabled status and last event ID in health checks.
    Flushes pending events on cleanup.

    Args:
        enabled: Whether Sentry is enabled (DSN configured).
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled
        logger.info("[SentrySubsystem] Initialized (enabled=%s)", enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def health_check(self) -> dict[str, Any]:
        """Return Sentry subsystem health status."""
        if not self._enabled:
            return {
                "status": "ok",
                "subsystem": "sentry",
                "enabled": False,
            }

        last_event_id: str | None = None
        try:
            import sentry_sdk

            raw_id = sentry_sdk.last_event_id()
            last_event_id = str(raw_id) if raw_id else None
        except Exception:
            pass

        return {
            "status": "ok",
            "subsystem": "sentry",
            "enabled": True,
            "last_event_id": last_event_id,
        }

    def cleanup(self) -> None:
        """Flush pending Sentry events."""
        if not self._enabled:
            return

        try:
            import sentry_sdk

            sentry_sdk.flush(timeout=2.0)
        except Exception:
            logger.debug("Failed to flush Sentry events", exc_info=True)
