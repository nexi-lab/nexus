"""Unit tests for PersistentService protocol (Issue #1577).

Verifies structural subtyping (Protocol) works correctly for service
lifecycle classification without requiring explicit inheritance.
"""

from __future__ import annotations

import pytest

from nexus.contracts.protocols.service_lifecycle import PersistentService

# ---------------------------------------------------------------------------
# Test stubs — satisfy protocols structurally (no inheritance)
# ---------------------------------------------------------------------------


class _FullPersistent:
    """Structurally satisfies PersistentService — start + stop."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _PlainService:
    """No lifecycle methods — not PersistentService."""

    def do_work(self) -> str:
        return "done"


class _PartialPersistent:
    """Has start but missing stop — NOT PersistentService."""

    async def start(self) -> None:
        pass


# ---------------------------------------------------------------------------
# PersistentService Protocol
# ---------------------------------------------------------------------------


class TestPersistentServiceProtocol:
    def test_full_implementation_detected(self) -> None:
        assert isinstance(_FullPersistent(), PersistentService)

    def test_plain_service_not_detected(self) -> None:
        assert not isinstance(_PlainService(), PersistentService)

    def test_partial_not_detected(self) -> None:
        """Missing stop() → not PersistentService."""
        assert not isinstance(_PartialPersistent(), PersistentService)

    @pytest.mark.asyncio()
    async def test_start_and_stop_are_async(self) -> None:
        svc = _FullPersistent()
        await svc.start()
        await svc.stop()
