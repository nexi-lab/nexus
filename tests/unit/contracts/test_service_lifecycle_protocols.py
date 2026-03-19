"""Unit tests for HotSwappable and PersistentService protocols (Issue #1577).

Verifies structural subtyping (Protocol) works correctly for service
lifecycle classification without requiring explicit inheritance.
"""

from __future__ import annotations

import pytest

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.protocols.service_lifecycle import HotSwappable, PersistentService

# ---------------------------------------------------------------------------
# Test stubs — satisfy protocols structurally (no inheritance)
# ---------------------------------------------------------------------------


class _FullHotSwappable:
    """Structurally satisfies HotSwappable — all 3 methods."""

    def hook_spec(self) -> HookSpec:
        return HookSpec(read_hooks=(object(),))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass


class _FullPersistent:
    """Structurally satisfies PersistentService — start + stop."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _BothProtocols:
    """Satisfies both HotSwappable AND PersistentService."""

    def hook_spec(self) -> HookSpec:
        return HookSpec()

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _PlainService:
    """No lifecycle methods — neither HotSwappable nor PersistentService."""

    def do_work(self) -> str:
        return "done"


class _PartialHotSwappable:
    """Has hook_spec and drain but missing activate — NOT HotSwappable."""

    def hook_spec(self) -> HookSpec:
        return HookSpec()

    async def drain(self) -> None:
        pass


class _PartialPersistent:
    """Has start but missing stop — NOT PersistentService."""

    async def start(self) -> None:
        pass


# ---------------------------------------------------------------------------
# HotSwappable Protocol
# ---------------------------------------------------------------------------


class TestHotSwappableProtocol:
    def test_full_implementation_detected(self) -> None:
        assert isinstance(_FullHotSwappable(), HotSwappable)

    def test_plain_service_not_detected(self) -> None:
        assert not isinstance(_PlainService(), HotSwappable)

    def test_partial_not_detected(self) -> None:
        """Missing activate() → not HotSwappable."""
        assert not isinstance(_PartialHotSwappable(), HotSwappable)

    def test_both_protocols_detected(self) -> None:
        svc = _BothProtocols()
        assert isinstance(svc, HotSwappable)
        assert isinstance(svc, PersistentService)

    @pytest.mark.asyncio()
    async def test_hook_spec_returns_hookspec(self) -> None:
        svc = _FullHotSwappable()
        spec = svc.hook_spec()
        assert isinstance(spec, HookSpec)
        assert spec.total_hooks == 1

    @pytest.mark.asyncio()
    async def test_drain_and_activate_are_async(self) -> None:
        svc = _FullHotSwappable()
        # Should be awaitable
        await svc.drain()
        await svc.activate()


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

    def test_hot_swappable_not_persistent(self) -> None:
        """HotSwappable without start/stop is NOT PersistentService."""
        assert not isinstance(_FullHotSwappable(), PersistentService)

    @pytest.mark.asyncio()
    async def test_start_and_stop_are_async(self) -> None:
        svc = _FullPersistent()
        await svc.start()
        await svc.stop()


# ---------------------------------------------------------------------------
# Four-quadrant classification
# ---------------------------------------------------------------------------


class TestFourQuadrant:
    """Verify the four-quadrant classification matrix."""

    def test_static_invocation(self) -> None:
        """Static + invocation-only: most common case."""
        svc = _PlainService()
        assert not isinstance(svc, HotSwappable)
        assert not isinstance(svc, PersistentService)

    def test_hot_swappable_invocation(self) -> None:
        """HotSwappable + invocation-only: can be swapped, no background tasks."""
        svc = _FullHotSwappable()
        assert isinstance(svc, HotSwappable)
        assert not isinstance(svc, PersistentService)

    def test_static_persistent(self) -> None:
        """Static + persistent: has background tasks but no hot-swap support."""
        svc = _FullPersistent()
        assert not isinstance(svc, HotSwappable)
        assert isinstance(svc, PersistentService)

    def test_hot_swappable_persistent(self) -> None:
        """HotSwappable + persistent: full lifecycle management."""
        svc = _BothProtocols()
        assert isinstance(svc, HotSwappable)
        assert isinstance(svc, PersistentService)
