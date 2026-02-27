"""Tests for SchedulerProtocol, AgentRequest, InMemoryScheduler,
CreditsReservationProtocol, and classify_agent_request (Issues #1383, #2360)."""

import dataclasses
from decimal import Decimal

import pytest

from nexus.services.protocols.scheduler import (
    _MAX_COMPLETED,
    AgentRequest,
    CreditsReservationProtocol,
    InMemoryScheduler,
    NullCreditsReservation,
    SchedulerProtocol,
    classify_agent_request,
)

# ---------------------------------------------------------------------------
# AgentRequest frozen dataclass tests
# ---------------------------------------------------------------------------


class TestAgentRequest:
    """Verify AgentRequest is a proper frozen, slots dataclass."""

    def test_frozen(self) -> None:
        req = AgentRequest(agent_id="a1", zone_id=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.agent_id = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(AgentRequest, "__slots__")

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(AgentRequest)}
        assert fields == {
            "agent_id",
            "zone_id",
            "priority",
            "submitted_at",
            "payload",
            # Astraea extensions (Issue #1274)
            "executor_id",
            "task_type",
            "request_state",
            "priority_class",
            "deadline",
            "boost_amount",
            "estimated_service_time",
            "idempotency_key",
        }

    def test_defaults(self) -> None:
        req = AgentRequest(agent_id="a1", zone_id=None)
        assert req.priority == 0
        assert req.submitted_at == ""
        assert req.payload == {}
        # Astraea defaults
        assert req.executor_id is None
        assert req.task_type == ""
        assert req.request_state == "pending"
        assert req.priority_class == "batch"
        assert req.deadline is None
        assert req.boost_amount == "0"
        assert req.estimated_service_time == 30.0

    def test_payload_default_factory(self) -> None:
        """Each instance gets its own dict, not a shared one."""
        r1 = AgentRequest(agent_id="a1", zone_id=None)
        r2 = AgentRequest(agent_id="a2", zone_id=None)
        assert r1.payload is not r2.payload

    def test_equality(self) -> None:
        kwargs = {
            "agent_id": "a1",
            "zone_id": "z1",
            "priority": 5,
            "submitted_at": "ts",
            "payload": {},
        }
        assert AgentRequest(**kwargs) == AgentRequest(**kwargs)


# ---------------------------------------------------------------------------
# Protocol structural tests
# ---------------------------------------------------------------------------


class TestSchedulerProtocol:
    def test_expected_methods(self) -> None:
        expected = {
            "submit",
            "next",
            "pending_count",
            "cancel",
            "get_status",
            "complete",
            "classify",
            "metrics",
            "initialize",
            "shutdown",
        }
        actual = {
            name
            for name in dir(SchedulerProtocol)
            if not name.startswith("_") and callable(getattr(SchedulerProtocol, name))
        }
        assert expected <= actual


# ---------------------------------------------------------------------------
# InMemoryScheduler conformance + functional tests
# ---------------------------------------------------------------------------


class TestInMemorySchedulerConformance:
    def test_isinstance_check(self) -> None:
        scheduler = InMemoryScheduler()
        assert isinstance(scheduler, SchedulerProtocol)

    def test_parameter_names_compatible(self) -> None:
        from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

        assert_protocol_conformance(InMemoryScheduler, SchedulerProtocol)


@pytest.mark.asyncio
class TestInMemorySchedulerFunctional:
    """Functional tests for the InMemoryScheduler test stub."""

    async def test_submit_and_next(self) -> None:
        scheduler = InMemoryScheduler()
        req = AgentRequest(agent_id="a1", zone_id="z1", submitted_at="t1")
        task_id = await scheduler.submit(req)
        assert isinstance(task_id, str)
        assert len(task_id) > 0
        result = await scheduler.next()
        assert result == req

    async def test_next_empty(self) -> None:
        scheduler = InMemoryScheduler()
        result = await scheduler.next()
        assert result is None

    async def test_fifo_ordering(self) -> None:
        scheduler = InMemoryScheduler()
        r1 = AgentRequest(agent_id="a1", zone_id=None, submitted_at="t1")
        r2 = AgentRequest(agent_id="a2", zone_id=None, submitted_at="t2")
        r3 = AgentRequest(agent_id="a3", zone_id=None, submitted_at="t3")

        await scheduler.submit(r1)
        await scheduler.submit(r2)
        await scheduler.submit(r3)

        assert await scheduler.next() == r1
        assert await scheduler.next() == r2
        assert await scheduler.next() == r3
        assert await scheduler.next() is None

    async def test_pending_count(self) -> None:
        scheduler = InMemoryScheduler()
        assert await scheduler.pending_count() == 0

        await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
        await scheduler.submit(AgentRequest(agent_id="a2", zone_id="z1"))
        await scheduler.submit(AgentRequest(agent_id="a3", zone_id="z2"))

        assert await scheduler.pending_count() == 3
        assert await scheduler.pending_count(zone_id="z1") == 2
        assert await scheduler.pending_count(zone_id="z2") == 1
        assert await scheduler.pending_count(zone_id="z3") == 0

    async def test_cancel(self) -> None:
        scheduler = InMemoryScheduler()
        await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
        await scheduler.submit(AgentRequest(agent_id="a2", zone_id="z1"))
        await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z2"))

        cancelled = await scheduler.cancel("a1")
        assert cancelled == 2
        assert await scheduler.pending_count() == 1

    async def test_cancel_nonexistent(self) -> None:
        scheduler = InMemoryScheduler()
        cancelled = await scheduler.cancel("no-such-agent")
        assert cancelled == 0

    async def test_get_status(self) -> None:
        scheduler = InMemoryScheduler()
        req = AgentRequest(agent_id="a1", zone_id="z1")
        task_id = await scheduler.submit(req)

        status = await scheduler.get_status(task_id)
        assert status is not None
        assert status["id"] == task_id
        assert status["status"] == "queued"

    async def test_get_status_nonexistent(self) -> None:
        scheduler = InMemoryScheduler()
        assert await scheduler.get_status("no-such-id") is None

    async def test_complete(self) -> None:
        scheduler = InMemoryScheduler()
        req = AgentRequest(agent_id="a1", zone_id="z1")
        task_id = await scheduler.submit(req)

        await scheduler.complete(task_id)
        status = await scheduler.get_status(task_id)
        assert status is not None
        assert status["status"] == "completed"

    async def test_complete_with_error(self) -> None:
        scheduler = InMemoryScheduler()
        req = AgentRequest(agent_id="a1", zone_id="z1")
        task_id = await scheduler.submit(req)

        await scheduler.complete(task_id, error="something broke")
        status = await scheduler.get_status(task_id)
        assert status is not None
        assert status["status"] == "failed"
        assert status["error_message"] == "something broke"

    async def test_classify(self) -> None:
        scheduler = InMemoryScheduler()
        req = AgentRequest(agent_id="a1", zone_id=None, priority=1)
        result = await scheduler.classify(req)
        assert isinstance(result, str)
        assert result in ("interactive", "batch", "background")

    async def test_metrics(self) -> None:
        scheduler = InMemoryScheduler()
        await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))

        m = await scheduler.metrics()
        assert isinstance(m, dict)
        assert "pending_count" in m

    async def test_metrics_shape_matches_production(self) -> None:
        """InMemoryScheduler.metrics() should return keys compatible with SchedulerService."""
        scheduler = InMemoryScheduler()
        m = await scheduler.metrics()
        assert "queue_by_class" in m
        assert "fair_share" in m
        assert "use_hrrn" in m
        assert m["use_hrrn"] is False

    async def test_lifecycle_methods_exist(self) -> None:
        """InMemoryScheduler has no-op lifecycle methods for production fallback."""
        scheduler = InMemoryScheduler()
        await scheduler.initialize()
        await scheduler.sync_fair_share()
        assert await scheduler.run_aging_sweep() == 0
        assert await scheduler.run_starvation_promotion() == 0
        await scheduler.shutdown()

    async def test_shutdown_clears_state(self) -> None:
        scheduler = InMemoryScheduler()
        await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
        assert await scheduler.pending_count() == 1
        await scheduler.shutdown()
        assert await scheduler.pending_count() == 0


# ---------------------------------------------------------------------------
# CreditsReservationProtocol tests (Issue #2360)
# ---------------------------------------------------------------------------


class TestCreditsReservationProtocol:
    """Verify CreditsReservationProtocol structural contract."""

    def test_expected_methods(self) -> None:
        expected = {"reserve", "release_reservation"}
        actual = {
            name
            for name in dir(CreditsReservationProtocol)
            if not name.startswith("_") and callable(getattr(CreditsReservationProtocol, name))
        }
        assert expected <= actual

    def test_null_credits_satisfies_protocol(self) -> None:
        null = NullCreditsReservation()
        assert isinstance(null, CreditsReservationProtocol)


@pytest.mark.asyncio
class TestNullCreditsReservation:
    """Verify NullCreditsReservation is a functional no-op."""

    async def test_reserve_returns_string(self) -> None:
        null = NullCreditsReservation()
        result = await null.reserve("agent-1", Decimal("10"))
        assert isinstance(result, str)
        assert result == "null-reservation"

    async def test_release_reservation_is_noop(self) -> None:
        null = NullCreditsReservation()
        await null.release_reservation("any-id")  # Should not raise


# ---------------------------------------------------------------------------
# classify_agent_request tests (Issue #2360 — DRY fix)
# ---------------------------------------------------------------------------


class TestClassifyAgentRequest:
    """Verify classify_agent_request shared helper."""

    def test_normal_priority_maps_to_batch(self) -> None:
        """PriorityTier.NORMAL (2) → 'batch'."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=2)
        assert classify_agent_request(req) == "batch"

    def test_critical_priority_maps_to_interactive(self) -> None:
        """PriorityTier.CRITICAL (0) → 'interactive'."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=0)
        assert classify_agent_request(req) == "interactive"

    def test_high_priority_maps_to_interactive(self) -> None:
        """PriorityTier.HIGH (1) → 'interactive'."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=1)
        assert classify_agent_request(req) == "interactive"

    def test_low_priority_maps_to_background(self) -> None:
        """PriorityTier.LOW (3) → 'background'."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=3)
        assert classify_agent_request(req) == "background"

    def test_best_effort_maps_to_background(self) -> None:
        """PriorityTier.BEST_EFFORT (4) → 'background'."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=4)
        assert classify_agent_request(req) == "background"

    def test_io_wait_promotes_background_to_batch(self) -> None:
        """BACKGROUND + IO_WAIT → 'batch' (IO promotion)."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=3, request_state="io_wait")
        assert classify_agent_request(req) == "batch"

    def test_invalid_priority_defaults_to_normal(self) -> None:
        """Unknown priority value → NORMAL → 'batch'."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=999)
        assert classify_agent_request(req) == "batch"

    def test_invalid_request_state_defaults_to_pending(self) -> None:
        """Unknown request_state → PENDING (no promotion)."""
        req = AgentRequest(agent_id="a1", zone_id=None, priority=3, request_state="unknown_state")
        assert classify_agent_request(req) == "background"


@pytest.mark.asyncio
class TestClassifyParity:
    """InMemoryScheduler.classify() must agree with classify_agent_request."""

    @pytest.mark.parametrize(
        "priority,request_state",
        [
            (0, "pending"),  # CRITICAL
            (1, "pending"),  # HIGH
            (2, "pending"),  # NORMAL
            (3, "pending"),  # LOW
            (4, "pending"),  # BEST_EFFORT
            (3, "io_wait"),  # IO promotion
            (0, "io_wait"),  # No promotion (already interactive)
            (999, "pending"),  # Invalid priority
            (2, "unknown"),  # Invalid state
        ],
    )
    async def test_classify_matches_shared_function(
        self, priority: int, request_state: str
    ) -> None:
        req = AgentRequest(
            agent_id="a1", zone_id=None, priority=priority, request_state=request_state
        )
        scheduler = InMemoryScheduler()
        scheduler_result = await scheduler.classify(req)
        shared_result = classify_agent_request(req)
        assert scheduler_result == shared_result, (
            f"Divergence for priority={priority}, state={request_state}: "
            f"scheduler={scheduler_result!r}, shared={shared_result!r}"
        )


# ---------------------------------------------------------------------------
# Bounded completed dict tests (Issue #2360 — performance)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInMemorySchedulerCompletedBound:
    """Verify _completed dict is bounded to _MAX_COMPLETED entries."""

    async def test_completed_eviction(self) -> None:
        scheduler = InMemoryScheduler()
        # Submit and complete _MAX_COMPLETED + 50 tasks
        task_ids = []
        for _ in range(_MAX_COMPLETED + 50):
            tid = await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
            task_ids.append(tid)
            await scheduler.next()  # Dequeue to allow completion
            await scheduler.complete(tid)

        # Dict should be bounded
        assert len(scheduler._completed) <= _MAX_COMPLETED
        # Most recent tasks should still be present
        assert await scheduler.get_status(task_ids[-1]) is not None

    async def test_no_eviction_at_boundary(self) -> None:
        """Exactly _MAX_COMPLETED entries → no eviction yet."""
        scheduler = InMemoryScheduler()
        task_ids = []
        for _ in range(_MAX_COMPLETED):
            tid = await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
            task_ids.append(tid)
            await scheduler.next()
            await scheduler.complete(tid)

        assert len(scheduler._completed) == _MAX_COMPLETED
        # First and last tasks should both be present
        assert await scheduler.get_status(task_ids[0]) is not None
        assert await scheduler.get_status(task_ids[-1]) is not None

    async def test_evicted_task_returns_none(self) -> None:
        """Tasks evicted from _completed return None from get_status."""
        scheduler = InMemoryScheduler()
        # Fill to boundary
        first_ids = []
        for _ in range(_MAX_COMPLETED):
            tid = await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
            first_ids.append(tid)
            await scheduler.next()
            await scheduler.complete(tid)

        # Add one more to trigger eviction of the oldest
        tid = await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
        await scheduler.next()
        await scheduler.complete(tid)

        # Oldest task should have been evicted
        assert await scheduler.get_status(first_ids[0]) is None
        # Newest task should still be accessible
        assert await scheduler.get_status(tid) is not None

    async def test_eviction_preserves_fifo_order(self) -> None:
        """Eviction removes the oldest entry (FIFO order)."""
        scheduler = InMemoryScheduler()
        task_ids = []
        for _ in range(_MAX_COMPLETED + 3):
            tid = await scheduler.submit(AgentRequest(agent_id="a1", zone_id="z1"))
            task_ids.append(tid)
            await scheduler.next()
            await scheduler.complete(tid)

        # First 3 should be evicted
        for i in range(3):
            assert await scheduler.get_status(task_ids[i]) is None
        # Fourth should still exist
        assert await scheduler.get_status(task_ids[3]) is not None
