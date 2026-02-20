"""Unit tests for ZoneLifecycleService (Issue #2061).

Tests phase transitions, finalizer orchestration, write-gating,
concurrency, timeouts, and edge cases.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.services.protocols.zone_lifecycle import ZonePhase
from nexus.services.zone_lifecycle import ZoneLifecycleService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finalizer(key: str, side_effect=None) -> MagicMock:
    """Create a mock finalizer implementing ZoneFinalizerProtocol."""
    f = MagicMock()
    f.finalizer_key = key
    f.finalize_zone = AsyncMock(side_effect=side_effect)
    return f


def _make_zone_model(zone_id="test-zone", phase="Active", finalizers="[]"):
    """Create a mock ZoneModel."""
    zone = MagicMock()
    zone.zone_id = zone_id
    zone.phase = phase
    zone.finalizers = finalizers
    return zone


def _make_session(zone=None):
    """Create a mock session that returns *zone* on .get()."""
    session = MagicMock()
    session.get.return_value = zone
    session.commit = MagicMock()
    session.scalars.return_value.all.return_value = []
    return session


def _make_session_factory():
    """Create a session factory mock."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_finalizer(self):
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f = _make_finalizer("nexus.core/cache")
        svc.register_finalizer(f)
        assert len(svc._finalizers) == 1

    def test_register_multiple_finalizers(self):
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        for key in ["nexus.core/cache", "nexus.core/search", "nexus.core/rebac"]:
            svc.register_finalizer(_make_finalizer(key))
        assert len(svc._finalizers) == 3

    def test_register_non_protocol_raises(self):
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        with pytest.raises(TypeError, match="Expected ZoneFinalizerProtocol"):
            svc.register_finalizer("not-a-finalizer")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Write-gating
# ---------------------------------------------------------------------------


class TestWriteGating:
    def test_not_terminating_by_default(self):
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        assert not svc.is_zone_terminating("any-zone")

    def test_terminating_after_add(self):
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        svc._terminating_zones.add("zone-1")
        assert svc.is_zone_terminating("zone-1")
        assert not svc.is_zone_terminating("zone-2")


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------


class TestPhaseTransitions:
    @pytest.mark.asyncio
    async def test_active_to_terminated_no_finalizers(self):
        """Empty zone (no registered finalizers) goes directly to Terminated."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        zone = _make_zone_model()
        session = _make_session(zone)

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATED
        assert zone.phase == ZonePhase.TERMINATED
        assert result.finalizers_pending == ()

    @pytest.mark.asyncio
    async def test_active_to_terminated_with_finalizers(self):
        """All finalizers succeed → zone reaches Terminated."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f1 = _make_finalizer("nexus.core/cache")
        f2 = _make_finalizer("nexus.core/rebac")
        svc.register_finalizer(f1)
        svc.register_finalizer(f2)

        zone = _make_zone_model()
        session = _make_session(zone)

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATED
        assert "nexus.core/cache" in result.finalizers_completed
        assert "nexus.core/rebac" in result.finalizers_completed
        assert result.finalizers_pending == ()
        f1.finalize_zone.assert_awaited_once_with("test-zone")
        f2.finalize_zone.assert_awaited_once_with("test-zone")

    @pytest.mark.asyncio
    async def test_already_terminated_returns_immediately(self):
        """Deprovision on Terminated zone is a no-op."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        zone = _make_zone_model(phase="Terminated")
        session = _make_session(zone)

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATED
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_zone_not_found(self):
        """Deprovision on non-existent zone returns Terminated with error."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        session = _make_session(zone=None)

        result = await svc.deprovision_zone("ghost-zone", session)

        assert result.phase == ZonePhase.TERMINATED
        assert "_" in result.finalizers_failed


# ---------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------


class TestPartialFailure:
    @pytest.mark.asyncio
    async def test_one_finalizer_fails_zone_stays_terminating(self):
        """If one finalizer fails, zone stays Terminating with pending list."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f_ok = _make_finalizer("nexus.core/cache")
        f_fail = _make_finalizer(
            "nexus.core/search", side_effect=RuntimeError("index corrupted")
        )
        f_rebac = _make_finalizer("nexus.core/rebac")
        svc.register_finalizer(f_ok)
        svc.register_finalizer(f_fail)
        svc.register_finalizer(f_rebac)

        zone = _make_zone_model()
        session = _make_session(zone)

        result = await svc.deprovision_zone("test-zone", session)

        # search failed → zone stays Terminating
        assert result.phase == ZonePhase.TERMINATING
        assert "nexus.core/search" in result.finalizers_pending
        assert "nexus.core/search" in result.finalizers_failed
        assert "nexus.core/cache" in result.finalizers_completed

    @pytest.mark.asyncio
    async def test_timeout_records_error(self):
        """A slow finalizer that exceeds timeout is recorded as failed."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())

        async def slow_finalize(zone_id: str) -> None:
            await asyncio.sleep(60)  # Will be cancelled by timeout

        f_slow = _make_finalizer("nexus.core/cache")
        f_slow.finalize_zone = AsyncMock(side_effect=slow_finalize)
        svc.register_finalizer(f_slow)

        zone = _make_zone_model()
        session = _make_session(zone)

        # Patch the timeout to be short for test
        with patch("nexus.services.zone_lifecycle._FINALIZER_TIMEOUT_S", 0.01):
            result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATING
        assert "nexus.core/cache" in result.finalizers_failed
        assert "timed out" in result.finalizers_failed["nexus.core/cache"]


# ---------------------------------------------------------------------------
# Concurrency ordering (Decision #13A)
# ---------------------------------------------------------------------------


class TestConcurrencyOrdering:
    @pytest.mark.asyncio
    async def test_rebac_runs_after_others(self):
        """ReBAC finalizer runs after all other finalizers complete."""
        call_order: list[str] = []

        async def record_call(key: str):
            async def _fn(zone_id: str) -> None:
                call_order.append(key)
                # Small delay to ensure concurrent tasks overlap
                await asyncio.sleep(0.01)
            return _fn

        svc = ZoneLifecycleService(session_factory=_make_session_factory())

        for key in ["nexus.core/cache", "nexus.core/search", "nexus.core/mount"]:
            f = _make_finalizer(key)
            f.finalize_zone = AsyncMock(side_effect=lambda zid, k=key: call_order.append(k))
            svc.register_finalizer(f)

        f_rebac = _make_finalizer("nexus.core/rebac")
        f_rebac.finalize_zone = AsyncMock(
            side_effect=lambda zid: call_order.append("nexus.core/rebac")
        )
        svc.register_finalizer(f_rebac)

        zone = _make_zone_model()
        session = _make_session(zone)

        await svc.deprovision_zone("test-zone", session)

        # ReBAC must be last
        assert call_order[-1] == "nexus.core/rebac"
        # Others ran before ReBAC
        for key in ["nexus.core/cache", "nexus.core/search", "nexus.core/mount"]:
            idx = call_order.index(key)
            assert idx < call_order.index("nexus.core/rebac")


# ---------------------------------------------------------------------------
# Write-gating integration
# ---------------------------------------------------------------------------


class TestWriteGatingIntegration:
    @pytest.mark.asyncio
    async def test_zone_added_to_terminating_set_during_deprovision(self):
        """Zone ID added to _terminating_zones when transitioning to Terminating."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        svc.register_finalizer(_make_finalizer("nexus.core/cache"))

        zone = _make_zone_model()
        session = _make_session(zone)

        # Before: not terminating
        assert not svc.is_zone_terminating("test-zone")

        result = await svc.deprovision_zone("test-zone", session)

        # After: Terminated → removed from set
        assert result.phase == ZonePhase.TERMINATED
        assert not svc.is_zone_terminating("test-zone")

    @pytest.mark.asyncio
    async def test_zone_stays_in_terminating_set_on_failure(self):
        """Zone stays in _terminating_zones if finalizer fails."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f_fail = _make_finalizer(
            "nexus.core/cache", side_effect=RuntimeError("boom")
        )
        svc.register_finalizer(f_fail)

        zone = _make_zone_model()
        session = _make_session(zone)

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATING
        assert svc.is_zone_terminating("test-zone")


# ---------------------------------------------------------------------------
# Load from DB
# ---------------------------------------------------------------------------


class TestLoadFromDB:
    def test_load_terminating_zones(self):
        """load_terminating_zones populates _terminating_zones from DB."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())

        session = MagicMock()
        session.scalars.return_value.all.return_value = ["zone-a", "zone-b"]

        svc.load_terminating_zones(session)

        assert svc.is_zone_terminating("zone-a")
        assert svc.is_zone_terminating("zone-b")
        assert not svc.is_zone_terminating("zone-c")


# ---------------------------------------------------------------------------
# Idempotency (Decision #11A)
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_retry_terminating_zone(self):
        """Re-deprovision a Terminating zone retries pending finalizers."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f = _make_finalizer("nexus.core/cache")
        svc.register_finalizer(f)

        zone = _make_zone_model(
            phase="Terminating",
            finalizers=json.dumps(["nexus.core/cache"]),
        )
        session = _make_session(zone)
        svc._terminating_zones.add("test-zone")

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATED
        assert "nexus.core/cache" in result.finalizers_completed
        f.finalize_zone.assert_awaited_once_with("test-zone")
