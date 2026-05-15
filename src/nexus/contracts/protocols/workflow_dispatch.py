"""Workflow dispatch service protocol (#625 partial: extract from core/).

Defines the contract for dispatching workflow trigger events and broadcasting
to webhook subscriptions. The implementation lives in
``nexus.services.workflow_dispatch_service.WorkflowDispatchService``.

Storage Affinity: DT_PIPE (Metastore ring buffer) + WorkflowEngine (RecordStore).
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WorkflowDispatchProtocol(Protocol):
    """Service contract for workflow event dispatch via DT_PIPE.

    ``fire()`` is sync (called from sync write/delete/rename hot paths).
    ``start()`` / ``stop()`` manage the background consumer task lifecycle.
    """

    def fire(self, trigger_type: str, event_context: dict[str, Any], label: str) -> None:
        """Fire a workflow event and broadcast to webhook subscriptions.

        Args:
            trigger_type: Trigger type (e.g. "file_write", "file_delete").
            event_context: Event payload dict.
            label: Human-readable label for logging (e.g. "file_write:/foo.txt").
        """
        ...

    async def start(self) -> None:
        """Create pipe and start background consumer task (idempotent)."""
        ...

    async def stop(self) -> None:
        """Cancel consumer task for graceful shutdown."""
        ...
