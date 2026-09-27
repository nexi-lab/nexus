"""Workflow engine protocol — tier-neutral contract (ops-scenario-matrix S28).

Defines the public contract for the workflow engine brick.
``WorkflowProtocol`` is the primary ops protocol.
``WorkflowServices`` and ``GlobMatchFn`` are DI helpers used by the engine.

``MetadataStoreProtocol`` and ``NexusOperationsProtocol`` remain in
``nexus.contracts.workflow_types`` — they are narrow service surfaces
consumed by workflow *actions*, not the engine contract itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from nexus.contracts.workflow_types import (
    MetadataStoreProtocol,
    NexusOperationsProtocol,
)


class GlobMatchFn(Protocol):
    """Callable that checks whether *path* matches any of *patterns*."""

    def __call__(self, path: str, patterns: list[str]) -> bool: ...


@runtime_checkable
class WorkflowProtocol(Protocol):
    """Public contract for the workflow engine brick."""

    async def fire_event(
        self,
        trigger_type: str,
        event_context: dict[str, Any],
    ) -> int: ...

    async def trigger_workflow(
        self,
        workflow_name: str,
        event_context: dict[str, Any],
    ) -> Any: ...

    def load_workflow(
        self,
        definition: Any,
        *,
        enabled: bool = True,
    ) -> bool: ...

    def unload_workflow(self, name: str) -> bool: ...

    def enable_workflow(self, name: str) -> None: ...

    def disable_workflow(self, name: str) -> None: ...

    def list_workflows(self) -> list[dict[str, Any]]: ...


@dataclass
class WorkflowServices:
    """Services injected into workflow context for action execution.

    All fields are optional — actions that need a missing service
    return ``ActionResult(success=False, error="… service not injected")``.
    """

    nexus_ops: NexusOperationsProtocol | None = None
    metadata_store: MetadataStoreProtocol | None = None
    glob_match: GlobMatchFn | None = None
