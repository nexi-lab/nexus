"""Hypothesis property-based tests for EventLog protocol invariants (Issue #1303).

Tests against an in-memory stub to validate the protocol contract.

Invariants proven:
  1. Sequence monotonicity: each append returns strictly increasing sequence
  2. Read ordering: events returned in sequence order
  3. Append-read roundtrip: appended events are always readable
"""

from __future__ import annotations

import asyncio
import uuid

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.services.protocols.event_log import EventId, EventLogProtocol, KernelEvent
from tests.strategies.kernel import kernel_event

# ---------------------------------------------------------------------------
# In-memory EventLog stub (protocol conformance target)
# ---------------------------------------------------------------------------


class InMemoryEventLog:
    """Minimal EventLog implementation for protocol invariant testing.

    Not production-grade — purely for validating the EventLogProtocol contract.
    """

    def __init__(self) -> None:
        self._events: list[KernelEvent] = []
        self._next_seq: int = 1

    async def append(self, event: KernelEvent) -> EventId:
        event_id = EventId(id=str(uuid.uuid4()), sequence=self._next_seq)
        # Store event with the assigned event_id
        stored = KernelEvent(
            type=event.type,
            source=event.source,
            zone_id=event.zone_id,
            timestamp=event.timestamp,
            event_id=event_id.id,
            payload=event.payload,
        )
        self._events.append(stored)
        self._next_seq += 1
        return event_id

    async def read(
        self,
        *,
        since_sequence: int = 0,
        limit: int = 100,
        zone_id: str | None = None,
    ) -> list[KernelEvent]:
        filtered = [
            e
            for i, e in enumerate(self._events, start=1)
            if i > since_sequence and (zone_id is None or e.zone_id == zone_id)
        ]
        return filtered[:limit]


# Verify protocol conformance at import time
assert isinstance(InMemoryEventLog(), EventLogProtocol)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously for Hypothesis compatibility."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Invariant 1: Sequence monotonicity
# ---------------------------------------------------------------------------


class TestEventLogSequenceMonotonicity:
    """Each append returns a strictly increasing sequence number."""

    @given(events=st.lists(kernel_event(), min_size=2, max_size=30))
    @settings(deadline=None)
    def test_sequence_strictly_increasing(self, events: list[KernelEvent]) -> None:
        """Appending N events produces sequence numbers 1, 2, ..., N."""

        async def _inner():
            log = InMemoryEventLog()
            sequences = []
            for event in events:
                eid = await log.append(event)
                sequences.append(eid.sequence)

            for i in range(len(sequences) - 1):
                assert sequences[i] < sequences[i + 1], (
                    f"Sequence not strictly increasing: {sequences}"
                )

        _run(_inner())

    @given(events=st.lists(kernel_event(), min_size=1, max_size=30))
    @settings(deadline=None)
    def test_sequence_starts_at_one(self, events: list[KernelEvent]) -> None:
        """First appended event always gets sequence 1."""

        async def _inner():
            log = InMemoryEventLog()
            eid = await log.append(events[0])
            assert eid.sequence == 1

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 2: Read ordering
# ---------------------------------------------------------------------------


class TestEventLogReadOrdering:
    """Events are returned in sequence (insertion) order."""

    @given(events=st.lists(kernel_event(), min_size=1, max_size=30))
    @settings(deadline=None)
    def test_read_returns_insertion_order(self, events: list[KernelEvent]) -> None:
        """read() returns events in the order they were appended."""

        async def _inner():
            log = InMemoryEventLog()
            appended_ids = []
            for event in events:
                eid = await log.append(event)
                appended_ids.append(eid.id)

            read_events = await log.read()
            read_ids = [e.event_id for e in read_events]

            assert read_ids == appended_ids[: len(read_ids)]

        _run(_inner())

    @given(
        events=st.lists(kernel_event(), min_size=5, max_size=30),
        since=st.data(),
    )
    @settings(deadline=None)
    def test_since_sequence_filters_correctly(
        self, events: list[KernelEvent], since: st.DataObject
    ) -> None:
        """read(since_sequence=N) returns only events with sequence > N."""
        cutoff = since.draw(st.integers(min_value=0, max_value=len(events)))

        async def _inner():
            log = InMemoryEventLog()
            for event in events:
                await log.append(event)

            read_events = await log.read(since_sequence=cutoff, limit=1000)
            assert len(read_events) == len(events) - cutoff

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 3: Append-read roundtrip
# ---------------------------------------------------------------------------


class TestEventLogRoundtrip:
    """Appended events are always readable."""

    @given(events=st.lists(kernel_event(), min_size=1, max_size=20))
    @settings(deadline=None)
    def test_all_appended_events_readable(self, events: list[KernelEvent]) -> None:
        """Every appended event can be read back."""

        async def _inner():
            log = InMemoryEventLog()
            for event in events:
                await log.append(event)

            read_events = await log.read(limit=1000)
            assert len(read_events) == len(events)

            for orig, read in zip(events, read_events):
                assert read.type == orig.type
                assert read.source == orig.source
                assert read.zone_id == orig.zone_id

        _run(_inner())
