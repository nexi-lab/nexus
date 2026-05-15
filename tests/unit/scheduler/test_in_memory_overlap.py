"""Tests for InMemoryScheduler overlap policy support (Issue #2749).

Verifies behavioral parity with SchedulerService for overlap policies.
"""

import pytest

from nexus.contracts.protocols.scheduler import AgentRequest
from nexus.services.scheduler.exceptions import TaskAlreadyRunning
from nexus.services.scheduler.in_memory import InMemoryScheduler


def _make_request(
    *,
    idempotency_key: str | None = None,
    overlap_policy: str = "skip",
    agent_id: str = "agent-a",
) -> AgentRequest:
    return AgentRequest(
        agent_id=agent_id,
        zone_id=None,
        priority=2,
        executor_id="exec-1",
        task_type="compute",
        idempotency_key=idempotency_key,
        overlap_policy=overlap_policy,
    )


class TestInMemorySkipPolicy:
    """SKIP policy in InMemoryScheduler."""

    @pytest.mark.asyncio
    async def test_skip_allows_when_no_running_task(self):
        sched = InMemoryScheduler()
        req = _make_request(idempotency_key="key-1", overlap_policy="skip")
        task_id = await sched.submit(req)
        assert task_id  # Should succeed

    @pytest.mark.asyncio
    async def test_skip_raises_when_task_running(self):
        sched = InMemoryScheduler()
        req1 = _make_request(idempotency_key="key-1", overlap_policy="skip")
        await sched.submit(req1)

        # Dequeue to mark as running
        await sched.next()

        # Second submit with same key should be rejected
        req2 = _make_request(idempotency_key="key-1", overlap_policy="skip")
        with pytest.raises(TaskAlreadyRunning, match="key-1"):
            await sched.submit(req2)

    @pytest.mark.asyncio
    async def test_skip_allows_after_completion(self):
        sched = InMemoryScheduler()
        req1 = _make_request(idempotency_key="key-1", overlap_policy="skip")
        task_id = await sched.submit(req1)
        await sched.next()  # Mark as running
        await sched.complete(task_id)  # Mark as completed

        # Should now allow re-submission
        req2 = _make_request(idempotency_key="key-1", overlap_policy="skip")
        task_id2 = await sched.submit(req2)
        assert task_id2  # Should succeed


class TestInMemoryCancelPreviousPolicy:
    """CANCEL_PREVIOUS policy in InMemoryScheduler."""

    @pytest.mark.asyncio
    async def test_cancel_previous_cancels_running_task(self):
        sched = InMemoryScheduler()
        req1 = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        task_id1 = await sched.submit(req1)
        await sched.next()  # Mark as running

        # Submit with CANCEL_PREVIOUS
        req2 = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        task_id2 = await sched.submit(req2)
        assert task_id2 != task_id1

        # Old task should be cancelled
        status = await sched.get_status(task_id1)
        assert status is not None
        assert status["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_previous_succeeds_when_no_running_task(self):
        sched = InMemoryScheduler()
        req = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        task_id = await sched.submit(req)
        assert task_id  # Should succeed without error


class TestInMemoryAllowPolicy:
    """ALLOW policy in InMemoryScheduler."""

    @pytest.mark.asyncio
    async def test_allow_always_enqueues(self):
        sched = InMemoryScheduler()
        req1 = _make_request(idempotency_key="key-1", overlap_policy="allow")
        await sched.submit(req1)
        await sched.next()  # Mark as running

        # Should succeed even though same key is running
        req2 = _make_request(idempotency_key="key-1", overlap_policy="allow")
        task_id2 = await sched.submit(req2)
        assert task_id2


class TestInMemoryNoKey:
    """Overlap policy ignored when idempotency_key is None."""

    @pytest.mark.asyncio
    async def test_no_key_always_enqueues(self):
        sched = InMemoryScheduler()
        req1 = _make_request(idempotency_key=None, overlap_policy="skip")
        task_id1 = await sched.submit(req1)

        req2 = _make_request(idempotency_key=None, overlap_policy="skip")
        task_id2 = await sched.submit(req2)

        assert task_id1 != task_id2  # Both should succeed
