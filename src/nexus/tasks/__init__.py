"""Nexus durable task queue engine (Tier 2).

Provides a durable, priority-aware task queue backed by the Rust task
engine in `services::tasks` (folded into the unified `nexus_runtime`
cdylib by Phase 3 restructure plan #6 — the standalone
`_nexus_tasks.so` is retired).  For ephemeral fire-and-forget tasks,
see the Tier 1 ARQ integration (#753).

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

from nexus_runtime import QueueStats, TaskEngine, TaskRecord

# Re-export the async runner.
from nexus.tasks.runner import AsyncTaskRunner

__all__ = [
    "AsyncTaskRunner",
    "QueueStats",
    "TaskEngine",
    "TaskRecord",
]
