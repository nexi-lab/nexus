"""Workflow brick protocols — zero-dependency interfaces.

All protocols are @runtime_checkable for duck-typed conformance.
No imports from nexus.core, nexus.storage, or nexus.server.

``MetadataStoreProtocol`` and ``NexusOperationsProtocol`` live in
``nexus.contracts.workflow_types`` (tier-neutral).
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# WorkflowLLMProtocol removed — LLM integration has been removed.
from nexus.contracts.workflow_types import (
    MetadataStoreProtocol,
    NexusOperationsProtocol,
)

__all__ = ["MetadataStoreProtocol", "NexusOperationsProtocol"]

# ---------------------------------------------------------------------------
# Dependency-injection callables
# ---------------------------------------------------------------------------


class GlobMatchFn(Protocol):
    """Callable that checks whether *path* matches any of *patterns*."""

    def __call__(self, path: str, patterns: list[str]) -> bool: ...


# ---------------------------------------------------------------------------
# Main brick protocol
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Services bundle injected into WorkflowContext
# ---------------------------------------------------------------------------


@runtime_checkable
class MountSyncProtocol(Protocol):
    """Narrow sync-only interface for workflow scheduled sync (Issue #3148).

    Exposes only what workflows need — sync a mount. Does NOT expose
    add_mount, remove_mount, or other mount lifecycle operations.
    MountService implements this.
    """

    async def sync_mount(
        self,
        mount_point: str | None = None,
        recursive: bool = True,
        generate_embeddings: bool = False,
    ) -> dict[str, Any]: ...


@dataclass
class WorkflowServices:
    """Services injected into workflow context for action execution.

    All fields are optional — actions that need a missing service
    return ``ActionResult(success=False, error="… service not injected")``.
    """

    nexus_ops: NexusOperationsProtocol | None = None
    metadata_store: MetadataStoreProtocol | None = None
    glob_match: GlobMatchFn | None = None
    mount_sync: MountSyncProtocol | None = None
