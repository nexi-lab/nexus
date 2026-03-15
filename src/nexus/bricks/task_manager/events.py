"""Task Manager event dataclasses and handler protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TaskCreatedEvent:
    """Emitted when a new task file is written for the first time."""

    task_id: str
    mission_id: str
    instruction: str
    worker_type: str | None
    blocked_by: list[str]
    input_refs: list[str]
    label: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskUpdatedEvent:
    """Emitted when an existing task file is overwritten."""

    task_id: str
    mission_id: str
    status: str
    worker_type: str | None
    label: str | None
    started_at: str | None
    completed_at: str | None
    timestamp: str  # when the hook fired


class TaskEventHandler(Protocol):
    """Protocol for objects that react to task lifecycle events."""

    def on_task_created(self, event: TaskCreatedEvent) -> None: ...
    def on_task_updated(self, event: TaskUpdatedEvent) -> None: ...
