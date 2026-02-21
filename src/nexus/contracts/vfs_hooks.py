"""VFS Hook contracts — context dataclasses and hook protocols.

These are inter-layer contracts used by the kernel (call sites in
``nexus_fs_core.py``) and implemented by service-layer hooks
(``services/hooks/``).  The kernel creates context objects after
each VFS operation and passes them to whichever pipeline is injected
via DI — the kernel never imports the pipeline class itself.

Lifecycle:
    read:   [kernel: validate → route → metadata → backend.read] → post_read hooks
    write:  [kernel: validate → route → backend.write → metadata.put] → post_write hooks
    delete: [kernel: validate → route → metadata.delete] → post_delete hooks
    rename: [kernel: validate → route → metadata.rename] → post_rename hooks

Issue #625: Extracted from ``core/vfs_hooks.py``.  Context dataclasses
and protocols are contracts (like ``WriteObserverProtocol``); the
pipeline dispatch class moved to ``services/hooks/pipeline.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.contracts.types import OperationWarning

if TYPE_CHECKING:
    from nexus.core.metadata import FileMetadata
    from nexus.core.permissions import OperationContext


# ---------------------------------------------------------------------------
# Hook context dataclasses — passed through pre/post hook chains
# ---------------------------------------------------------------------------


@dataclass
class ReadHookContext:
    """Context passed through read hooks."""

    path: str
    context: OperationContext | None
    zone_id: str | None = None
    agent_id: str | None = None
    metadata: FileMetadata | None = None
    content: bytes | None = None
    content_hash: str | None = None
    warnings: list[OperationWarning] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteHookContext:
    """Context passed through write hooks."""

    path: str
    content: bytes
    context: OperationContext | None
    zone_id: str | None = None
    agent_id: str | None = None
    is_new_file: bool = False
    content_hash: str | None = None
    metadata: FileMetadata | None = None
    old_metadata: FileMetadata | None = None
    new_version: int = 1
    snapshot_hash: str | None = None
    metadata_snapshot: dict[str, Any] | None = None
    warnings: list[OperationWarning] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeleteHookContext:
    """Context passed through delete hooks."""

    path: str
    context: OperationContext | None
    zone_id: str | None = None
    agent_id: str | None = None
    metadata: FileMetadata | None = None
    warnings: list[OperationWarning] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RenameHookContext:
    """Context passed through rename hooks."""

    old_path: str
    new_path: str
    context: OperationContext | None
    zone_id: str | None = None
    agent_id: str | None = None
    is_directory: bool = False
    metadata: FileMetadata | None = None
    warnings: list[OperationWarning] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Hook protocols — implemented by services that plug into VFS operations
# ---------------------------------------------------------------------------


@runtime_checkable
class VFSReadHook(Protocol):
    """Hook that runs after a read operation."""

    @property
    def name(self) -> str: ...

    def on_post_read(self, ctx: ReadHookContext) -> None:
        """Called after content is read from backend.

        Implementations may:
        - Filter/transform ctx.content (e.g., dynamic viewer filter)
        - Record read tracking (e.g., dependency tracker)
        - Register cache read sets
        - Append warnings to ctx.warnings
        """
        ...


@runtime_checkable
class VFSWriteHook(Protocol):
    """Hook that runs after a write operation."""

    @property
    def name(self) -> str: ...

    def on_post_write(self, ctx: WriteHookContext) -> None:
        """Called after content is written and metadata stored.

        Implementations may:
        - Update tiger cache bitmaps
        - Advance zone revision
        - Notify observers (audit trail)
        - Queue deferred permissions
        - Auto-parse file content
        - Append warnings to ctx.warnings
        """
        ...


@runtime_checkable
class VFSDeleteHook(Protocol):
    """Hook that runs after a delete operation."""

    @property
    def name(self) -> str: ...

    def on_post_delete(self, ctx: DeleteHookContext) -> None: ...


@runtime_checkable
class VFSRenameHook(Protocol):
    """Hook that runs after a rename operation."""

    @property
    def name(self) -> str: ...

    def on_post_rename(self, ctx: RenameHookContext) -> None: ...
