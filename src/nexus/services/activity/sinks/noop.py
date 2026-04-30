"""Noop sink — accepts all writes, persists nothing."""

from __future__ import annotations

from collections.abc import Sequence

from nexus.services.activity.events import ActivityEvent


class NoopSink:
    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:  # noqa: ARG002
        return None

    async def close(self) -> None:
        return None
