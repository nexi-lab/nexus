"""Hypothesis property-based tests for Scheduler kernel invariants (Issue #1303).

Invariants proven:
  1. No starvation: all submitted requests are eventually returned by next()
  2. Priority ordering: higher priority always scheduled before lower
  3. Cancel correctness: cancelled requests never returned by next()
  4. Pending count accuracy: pending_count == submitted - consumed - cancelled
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.services.protocols.scheduler import AgentRequest, InMemoryScheduler
from tests.strategies.kernel import agent_request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously for Hypothesis compatibility."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Invariant 1: No starvation
# ---------------------------------------------------------------------------


class TestSchedulerNoStarvation:
    """Every submitted request is eventually returned."""

    @given(requests=st.lists(agent_request(), min_size=1, max_size=50))
    @settings(deadline=None)
    def test_all_submitted_are_eventually_returned(self, requests: list[AgentRequest]) -> None:
        """Submit N requests, call next() N times, get all N back."""

        async def _inner():
            scheduler = InMemoryScheduler()
            for req in requests:
                await scheduler.submit(req)

            returned = []
            for _ in range(len(requests)):
                result = await scheduler.next()
                assert result is not None
                returned.append(result)

            # Exact count: submitted N, returned N
            assert len(returned) == len(requests)

            # Every returned request is one of the submitted requests
            # (compare by identity via (agent_id, priority, submitted_at) tuple)
            submitted_keys = sorted((r.agent_id, r.priority, r.submitted_at) for r in requests)
            returned_keys = sorted((r.agent_id, r.priority, r.submitted_at) for r in returned)
            assert submitted_keys == returned_keys

            # Queue should be empty
            assert await scheduler.next() is None

        _run(_inner())

    @given(requests=st.lists(agent_request(), min_size=0, max_size=20))
    @settings(deadline=None)
    def test_next_returns_none_when_empty(self, requests: list[AgentRequest]) -> None:
        """next() returns None after all requests consumed."""

        async def _inner():
            scheduler = InMemoryScheduler()
            for req in requests:
                await scheduler.submit(req)
            for _ in range(len(requests)):
                await scheduler.next()

            assert await scheduler.next() is None

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 2: Priority ordering
# ---------------------------------------------------------------------------


class TestSchedulerPriorityOrdering:
    """Higher priority requests are scheduled before lower priority."""

    @given(
        priorities=st.lists(
            st.integers(min_value=0, max_value=100),
            min_size=2,
            max_size=30,
        ),
    )
    @settings(deadline=None)
    def test_dequeue_order_respects_priority(self, priorities: list[int]) -> None:
        """Requests dequeued in priority order (high → low)."""

        async def _inner():
            scheduler = InMemoryScheduler()
            for i, prio in enumerate(priorities):
                req = AgentRequest(
                    agent_id=f"agent_{i}",
                    zone_id=None,
                    priority=prio,
                )
                await scheduler.submit(req)

            returned_priorities = []
            for _ in range(len(priorities)):
                result = await scheduler.next()
                assert result is not None
                returned_priorities.append(result.priority)

            # Must be non-increasing (highest first)
            for i in range(len(returned_priorities) - 1):
                assert returned_priorities[i] >= returned_priorities[i + 1], (
                    f"Priority order violated: {returned_priorities}"
                )

        _run(_inner())

    @given(
        n=st.integers(min_value=2, max_value=20),
        priority=st.integers(min_value=0, max_value=100),
    )
    @settings(deadline=None)
    def test_equal_priority_fifo(self, n: int, priority: int) -> None:
        """Equal-priority requests are dequeued in FIFO order."""

        async def _inner():
            scheduler = InMemoryScheduler()
            for i in range(n):
                req = AgentRequest(
                    agent_id=f"agent_{i}",
                    zone_id=None,
                    priority=priority,
                )
                await scheduler.submit(req)

            returned_ids = []
            for _ in range(n):
                result = await scheduler.next()
                assert result is not None
                returned_ids.append(result.agent_id)

            expected_ids = [f"agent_{i}" for i in range(n)]
            assert returned_ids == expected_ids

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 3: Cancel correctness
# ---------------------------------------------------------------------------


class TestSchedulerCancelInvariants:
    """Cancelled requests are never returned."""

    @given(
        requests=st.lists(agent_request(), min_size=1, max_size=30),
        cancel_idx=st.data(),
    )
    @settings(deadline=None)
    def test_cancelled_never_returned(
        self,
        requests: list[AgentRequest],
        cancel_idx: st.DataObject,
    ) -> None:
        """After cancel(agent_id), no request with that agent_id is returned."""
        # Pick a random agent to cancel
        idx = cancel_idx.draw(st.integers(min_value=0, max_value=len(requests) - 1))
        cancelled_id = requests[idx].agent_id

        async def _inner():
            scheduler = InMemoryScheduler()
            for req in requests:
                await scheduler.submit(req)

            await scheduler.cancel(cancelled_id)

            # Drain all remaining
            returned = []
            while True:
                result = await scheduler.next()
                if result is None:
                    break
                returned.append(result)

            for r in returned:
                assert r.agent_id != cancelled_id, f"Cancelled agent {cancelled_id} was returned"

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 4: Pending count accuracy
# ---------------------------------------------------------------------------


class TestSchedulerPendingCountInvariants:
    """Pending count is always accurate."""

    @given(requests=st.lists(agent_request(), min_size=0, max_size=30))
    @settings(deadline=None)
    def test_pending_count_after_submit(self, requests: list[AgentRequest]) -> None:
        """pending_count() == number of submitted requests."""

        async def _inner():
            scheduler = InMemoryScheduler()
            for req in requests:
                await scheduler.submit(req)
            assert await scheduler.pending_count() == len(requests)

        _run(_inner())

    @given(
        requests=st.lists(agent_request(), min_size=1, max_size=30),
        consume_count=st.data(),
    )
    @settings(deadline=None)
    def test_pending_count_after_consume(
        self,
        requests: list[AgentRequest],
        consume_count: st.DataObject,
    ) -> None:
        """pending_count() == submitted - consumed."""
        n_consume = consume_count.draw(st.integers(min_value=0, max_value=len(requests)))

        async def _inner():
            scheduler = InMemoryScheduler()
            for req in requests:
                await scheduler.submit(req)

            for _ in range(n_consume):
                await scheduler.next()

            expected = len(requests) - n_consume
            assert await scheduler.pending_count() == expected

        _run(_inner())

    @given(
        zone_requests=st.lists(
            st.tuples(
                st.sampled_from(["zone_a", "zone_b", "zone_c"]),
                agent_request(),
            ),
            min_size=1,
            max_size=30,
        ),
    )
    @settings(deadline=None)
    def test_pending_count_with_zone_filter(
        self, zone_requests: list[tuple[str, AgentRequest]]
    ) -> None:
        """pending_count(zone_id=X) == count of requests with that zone_id."""

        async def _inner():
            scheduler = InMemoryScheduler()
            zone_counts: dict[str, int] = {}

            for zone_id, req in zone_requests:
                # Override zone_id on the request
                zoned_req = AgentRequest(
                    agent_id=req.agent_id,
                    zone_id=zone_id,
                    priority=req.priority,
                    submitted_at=req.submitted_at,
                    payload=req.payload,
                )
                await scheduler.submit(zoned_req)
                zone_counts[zone_id] = zone_counts.get(zone_id, 0) + 1

            for zone_id, expected in zone_counts.items():
                actual = await scheduler.pending_count(zone_id=zone_id)
                assert actual == expected, f"Zone {zone_id}: expected {expected}, got {actual}"

        _run(_inner())
