"""Task Manager brick — NexusFS-backed task and mission management."""

from nexus.bricks.task_manager.dispatch_consumer import TaskDispatchPipeConsumer
from nexus.bricks.task_manager.events import TaskSignalHandler
from nexus.bricks.task_manager.service import TaskManagerService
from nexus.bricks.task_manager.write_hook import TaskWriteHook

__all__ = [
    "TaskDispatchPipeConsumer",
    "TaskManagerService",
    "TaskSignalHandler",
    "TaskWriteHook",
]
