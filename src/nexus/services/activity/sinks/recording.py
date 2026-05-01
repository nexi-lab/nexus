"""Recording sink — keeps every received event in memory. Tests only."""

from __future__ import annotations

from collections.abc import Sequence

from nexus.services.activity.events import ActivityEvent, EventKind


class RecordingSink:
    def __init__(self) -> None:
        self._events: list[ActivityEvent] = []

    @property
    def events(self) -> list[ActivityEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    def events_of(self, kind: EventKind) -> list[ActivityEvent]:
        return [e for e in self._events if e.kind is kind]

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
        self._events.extend(events)

    async def close(self) -> None:
        return None
