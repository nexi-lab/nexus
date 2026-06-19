"""Nexus durable task queue engine (Tier 2).

Provides a durable, priority-aware task queue backed by the Rust task
engine running inside the nexus-cluster process. Tasks are managed
via gRPC Call RPC.

Usage:
    from nexus.tasks import TaskEngine, AsyncTaskRunner

    engine = TaskEngine("/tmp/nexus-tasks")
    runner = AsyncTaskRunner(engine)

    @runner.register("my_task")
    async def handle_my_task(params: bytes, progress):
        await progress.update(50, "halfway")
        return b"done"

    # In your event loop:
    await runner.run()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus.tasks.runner import AsyncTaskRunner


@dataclass
class TaskRecord:
    """Task record — mirrors the Rust TaskRecord struct."""

    task_id: str = ""
    queue: str = ""
    payload: bytes = b""
    status: str = "pending"
    priority: int = 0
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str | None = None


@dataclass
class QueueStats:
    """Queue statistics — mirrors the Rust QueueStats struct."""

    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    total: int = 0


class TaskEngine:
    """Task engine client — delegates to the kernel process via gRPC."""

    def __init__(self, path: str = "") -> None:
        self._path = path

    def enqueue(self, queue: str, payload: bytes, priority: int = 0) -> str:
        """Enqueue a task. Returns task_id."""
        raise NotImplementedError("TaskEngine requires kernel gRPC connection")

    def dequeue(self, queue: str) -> TaskRecord | None:
        """Dequeue next task from queue."""
        raise NotImplementedError("TaskEngine requires kernel gRPC connection")

    def complete(self, task_id: str, result: bytes = b"") -> None:
        """Mark task as completed."""
        raise NotImplementedError("TaskEngine requires kernel gRPC connection")

    def fail(self, task_id: str, error: str) -> None:
        """Mark task as failed."""
        raise NotImplementedError("TaskEngine requires kernel gRPC connection")

    def stats(self, queue: str) -> QueueStats:  # noqa: ARG002
        """Get queue statistics."""
        return QueueStats()


__all__ = [
    "AsyncTaskRunner",
    "QueueStats",
    "TaskEngine",
    "TaskRecord",
]
