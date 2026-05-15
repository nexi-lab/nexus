"""Unit tests for ZoneLifecycleService (Issue #2061).

Tests phase transitions, finalizer orchestration, write-gating,
concurrency, timeouts, and edge cases.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.protocols.zone_lifecycle import ZonePhase
from nexus.services.lifecycle.zone_lifecycle import ZoneLifecycleService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finalizer(key: str, side_effect=None) -> MagicMock:
    """Create a mock finalizer implementing ZoneFinalizerProtocol."""
    f = MagicMock()
    f.finalizer_key = key
    f.finalize_zone = AsyncMock(side_effect=side_effect)
    return f


class _FakeZone:
    """Lightweight zone mock with dynamic parsed_finalizers."""

    def __init__(self, zone_id="test-zone", phase="Active", finalizers="[]"):
        self.zone_id = zone_id
        self.phase = phase
        self.finalizers = finalizers
        self.deleted_at = None

    @property
    def parsed_finalizers(self) -> list[str]:
        return json.loads(self.finalizers)


def _make_zone_model(zone_id="test-zone", phase="Active", finalizers="[]"):
    """Create a fake ZoneModel with dynamic parsed_finalizers."""
    return _FakeZone(zone_id=zone_id, phase=phase, finalizers=finalizers)


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


class TestReservedZoneGuard:
    """Issue #3897: deprovisioning the default ROOT_ZONE_ID must be refused."""

    @pytest.mark.asyncio
    async def test_root_zone_deprovision_raises_value_error(self):
        from nexus.contracts.constants import ROOT_ZONE_ID

        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        zone = _make_zone_model(zone_id=ROOT_ZONE_ID)
        session = _make_session(zone)

        with pytest.raises(ValueError, match=f"reserved zone {ROOT_ZONE_ID!r}"):
            await svc.deprovision_zone(ROOT_ZONE_ID, session)

        # Zone state untouched: phase remains Active, no commit attempted.
        assert zone.phase == "Active"
        session.commit.assert_not_called()


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
        f_fail = _make_finalizer("nexus.core/search", side_effect=RuntimeError("index corrupted"))
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
        with patch("nexus.services.lifecycle.zone_lifecycle._FINALIZER_TIMEOUT_S", 0.01):
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
        """ReBAC finalizer (phase=sequential) runs after all concurrent finalizers."""
        call_order: list[str] = []

        svc = ZoneLifecycleService(session_factory=_make_session_factory())

        for key in ["nexus.core/cache", "nexus.core/search", "nexus.core/mount"]:
            f = _make_finalizer(key)
            f.finalize_zone = AsyncMock(side_effect=lambda zid, k=key: call_order.append(k))
            svc.register_finalizer(f, phase="concurrent")

        f_rebac = _make_finalizer("nexus.core/rebac")
        f_rebac.finalize_zone = AsyncMock(
            side_effect=lambda zid: call_order.append("nexus.core/rebac")
        )
        svc.register_finalizer(f_rebac, phase="sequential")

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
        f_fail = _make_finalizer("nexus.core/cache", side_effect=RuntimeError("boom"))
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


# ---------------------------------------------------------------------------
# Orphaned finalizer keys (#11A — Issue #2070)
# ---------------------------------------------------------------------------


class TestOrphanedFinalizers:
    @pytest.mark.asyncio
    async def test_orphaned_key_stays_pending(self):
        """A finalizer key in the DB but no registered handler stays pending."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        # Register only cache — "nexus.core/orphan" has no handler
        svc.register_finalizer(_make_finalizer("nexus.core/cache"))

        zone = _make_zone_model(
            phase="Terminating",
            finalizers=json.dumps(["nexus.core/cache", "nexus.core/orphan"]),
        )
        session = _make_session(zone)
        svc._terminating_zones.add("test-zone")

        result = await svc.deprovision_zone("test-zone", session)

        # cache succeeded, orphan has no handler → stays pending → Terminating
        assert result.phase == ZonePhase.TERMINATING
        assert "nexus.core/cache" in result.finalizers_completed
        assert "nexus.core/orphan" in result.finalizers_pending

    @pytest.mark.asyncio
    async def test_only_orphaned_keys_stays_terminating(self):
        """Zone with only orphaned keys stays Terminating forever."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())

        zone = _make_zone_model(
            phase="Terminating",
            finalizers=json.dumps(["nexus.core/unknown"]),
        )
        session = _make_session(zone)
        svc._terminating_zones.add("test-zone")

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATING
        assert "nexus.core/unknown" in result.finalizers_pending


# ---------------------------------------------------------------------------
# deleted_at set on Terminated (#3A — Issue #2070)
# ---------------------------------------------------------------------------


class TestDeletedAtTimestamp:
    @pytest.mark.asyncio
    async def test_deleted_at_set_on_termination(self):
        """Zone.deleted_at is set when phase transitions to Terminated."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        zone = _make_zone_model()
        session = _make_session(zone)

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATED
        assert zone.deleted_at is not None

    @pytest.mark.asyncio
    async def test_deleted_at_not_set_on_partial_failure(self):
        """Zone.deleted_at stays None when finalizers fail (still Terminating)."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f_fail = _make_finalizer("nexus.core/cache", side_effect=RuntimeError("boom"))
        svc.register_finalizer(f_fail)

        zone = _make_zone_model()
        session = _make_session(zone)

        result = await svc.deprovision_zone("test-zone", session)

        assert result.phase == ZonePhase.TERMINATING
        assert zone.deleted_at is None


# ---------------------------------------------------------------------------
# Phase parameter in registration (#4B — Issue #2070)
# ---------------------------------------------------------------------------


class TestFinalizerPhaseRegistration:
    def test_default_phase_is_concurrent(self):
        """Finalizers registered without phase default to 'concurrent'."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f = _make_finalizer("nexus.core/cache")
        svc.register_finalizer(f)
        _fin, phase = svc._finalizers[0]
        assert phase == "concurrent"

    def test_explicit_sequential_phase(self):
        """Finalizers can be registered with phase='sequential'."""
        svc = ZoneLifecycleService(session_factory=_make_session_factory())
        f = _make_finalizer("nexus.core/rebac")
        svc.register_finalizer(f, phase="sequential")
        _fin, phase = svc._finalizers[0]
        assert phase == "sequential"

    @pytest.mark.asyncio
    async def test_sequential_after_concurrent(self):
        """Sequential finalizers run after all concurrent ones complete."""
        call_order: list[str] = []

        svc = ZoneLifecycleService(session_factory=_make_session_factory())

        f_concurrent = _make_finalizer("nexus.core/cache")
        f_concurrent.finalize_zone = AsyncMock(side_effect=lambda zid: call_order.append("cache"))
        svc.register_finalizer(f_concurrent, phase="concurrent")

        f_seq = _make_finalizer("nexus.core/rebac")
        f_seq.finalize_zone = AsyncMock(side_effect=lambda zid: call_order.append("rebac"))
        svc.register_finalizer(f_seq, phase="sequential")

        zone = _make_zone_model()
        session = _make_session(zone)

        await svc.deprovision_zone("test-zone", session)

        assert call_order == ["cache", "rebac"]
