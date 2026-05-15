"""Unit tests for BackgroundService protocol (Issue #1577).

Verifies structural subtyping (Protocol) works correctly for service
lifecycle classification without requiring explicit inheritance.
"""

from __future__ import annotations

import pytest

from nexus.contracts.protocols.service_lifecycle import BackgroundService

# ---------------------------------------------------------------------------
# Test stubs — satisfy protocols structurally (no inheritance)
# ---------------------------------------------------------------------------


class _FullBackground:
    """Structurally satisfies BackgroundService — start + stop."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _PlainService:
    """No lifecycle methods — not BackgroundService."""

    def do_work(self) -> str:
        return "done"


class _PartialBackground:
    """Has start but missing stop — NOT BackgroundService."""

    async def start(self) -> None:
        pass


# ---------------------------------------------------------------------------
# BackgroundService Protocol
# ---------------------------------------------------------------------------


class TestBackgroundServiceProtocol:
    def test_full_implementation_detected(self) -> None:
        assert isinstance(_FullBackground(), BackgroundService)

    def test_plain_service_not_detected(self) -> None:
        assert not isinstance(_PlainService(), BackgroundService)

    def test_partial_not_detected(self) -> None:
        """Missing stop() → not BackgroundService."""
        assert not isinstance(_PartialBackground(), BackgroundService)

    @pytest.mark.asyncio()
    async def test_start_and_stop_are_async(self) -> None:
        svc = _FullBackground()
        await svc.start()
        await svc.stop()
