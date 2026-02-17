"""Workflow brick protocols — zero-dependency interfaces.

All protocols are @runtime_checkable for duck-typed conformance.
No imports from nexus.core, nexus.storage, or nexus.server.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Dependency-injection callables
# ---------------------------------------------------------------------------

class GlobMatchFn(Protocol):
    """Callable that checks whether *path* matches any of *patterns*."""

    def __call__(self, path: str, patterns: list[str]) -> bool: ...

# ---------------------------------------------------------------------------
# Service protocols (what actions need)
# ---------------------------------------------------------------------------

@runtime_checkable
class NexusOperationsProtocol(Protocol):
    """Filesystem operations that workflow actions may invoke."""

    async def parse(self, path: str, *, parser: str = "auto") -> Any: ...

    async def add_tag(self, path: str, tag: str) -> None: ...

    async def remove_tag(self, path: str, tag: str) -> None: ...

    def rename(self, old_path: str, new_path: str) -> None: ...

    def mkdir(self, path: str, *, parents: bool = False) -> None: ...

    def read(self, path: str) -> bytes: ...

@runtime_checkable
class MetadataStoreProtocol(Protocol):
    """Minimal metadata store surface used by MetadataAction."""

    def get_path(self, path: str) -> Any: ...

    def set_file_metadata(self, path_id: Any, key: str, value: str) -> None: ...

@runtime_checkable
class LLMProviderProtocol(Protocol):
    """LLM provider surface used by LLMAction."""

    async def generate(self, *, model: str, prompt: str, system: str) -> str: ...

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

@dataclass
class WorkflowServices:
    """Services injected into workflow context for action execution.

    All fields are optional — actions that need a missing service
    return ``ActionResult(success=False, error="… service not injected")``.
    """

    nexus_ops: NexusOperationsProtocol | None = None
    metadata_store: MetadataStoreProtocol | None = None
    llm_provider: LLMProviderProtocol | None = None
    glob_match: GlobMatchFn | None = None
