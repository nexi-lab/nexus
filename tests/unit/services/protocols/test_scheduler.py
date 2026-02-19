"""Tests for SchedulerProtocol, AgentRequest, and InMemoryScheduler (Issue #1383)."""

from __future__ import annotations

import dataclasses

import pytest

from nexus.services.protocols.scheduler import (
    AgentRequest,
    InMemoryScheduler,
    SchedulerProtocol,
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
        assert status["error"] == "something broke"

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
