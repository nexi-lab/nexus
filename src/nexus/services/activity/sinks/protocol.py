"""Sink protocol for the activity worker."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from nexus.services.activity.events import ActivityEvent


@runtime_checkable
class SinkProtocol(Protocol):
    """Where the ActivityWorker flushes batches.

    Implementations must be safe to call concurrently with close(): the
    worker serializes write_batch calls but may issue close() from
    a different task.
    """

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None: ...

    async def close(self) -> None: ...
