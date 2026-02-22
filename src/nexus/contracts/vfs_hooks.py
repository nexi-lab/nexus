"""VFS Hook contracts — context dataclasses and hook protocols.

These are inter-layer contracts used by the kernel (call sites in
``nexus_fs_core.py`` / ``nexus_fs.py``) and implemented by service-layer
hooks.  The kernel creates context objects after each VFS operation
and passes them to ``KernelDispatch`` (injected via DI).

Two-phase dispatch model (Issue #900):
    INTERCEPT — synchronous, ordered.  Can abort (raise) or modify context.
    OBSERVE   — fire-and-forget (``MutationEvent``).  Cannot abort.

All six operations are covered:
    read / write / delete / rename / mkdir / rmdir

Issue #625: Extracted from ``core/vfs_hooks.py``.
Issue #900: Added MkdirHookContext, RmdirHookContext, VFSMkdirHook,
            VFSRmdirHook.  Unified under KernelDispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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


@dataclass
class MkdirHookContext:
    """Context passed through mkdir hooks (Issue #900)."""

    path: str
    context: OperationContext | None
    zone_id: str | None = None
    agent_id: str | None = None
    metadata: FileMetadata | None = None
    warnings: list[OperationWarning] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RmdirHookContext:
    """Context passed through rmdir hooks (Issue #900)."""

    path: str
    context: OperationContext | None
    zone_id: str | None = None
    agent_id: str | None = None
    recursive: bool = False
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


@runtime_checkable
class VFSMkdirHook(Protocol):
    """Hook that runs after a mkdir operation (Issue #900)."""

    @property
    def name(self) -> str: ...

    def on_post_mkdir(self, ctx: MkdirHookContext) -> None: ...


@runtime_checkable
class VFSRmdirHook(Protocol):
    """Hook that runs after a rmdir operation (Issue #900)."""

    @property
    def name(self) -> str: ...

    def on_post_rmdir(self, ctx: RmdirHookContext) -> None: ...


# ---------------------------------------------------------------------------
# OBSERVE phase — mutation event + observer protocol (Issue #900)
# ---------------------------------------------------------------------------


class MutationOp(Enum):
    """Kernel VFS mutation operation types."""

    WRITE = "write"
    DELETE = "delete"
    RENAME = "rename"
    MKDIR = "mkdir"
    RMDIR = "rmdir"


@dataclass(frozen=True, slots=True)
class MutationEvent:
    """Frozen event passed to OBSERVE-phase observers after a VFS mutation.

    Carries all context that observers might need.  Each observer
    extracts what it requires and ignores the rest.
    """

    operation: MutationOp
    path: str
    zone_id: str
    revision: int

    # Common optional context
    agent_id: str | None = None
    user_id: str | None = None
    timestamp: str | None = None
    etag: str | None = None
    size: int | None = None

    # Write-specific
    version: int | None = None
    is_new: bool = False

    # Rename-specific
    new_path: str | None = None


@runtime_checkable
class VFSObserver(Protocol):
    """OBSERVE-phase observer for kernel VFS mutations (fire-and-forget).

    Receives a frozen ``MutationEvent`` after every successful mutation.
    Must not raise — exceptions are caught and logged by KernelDispatch.
    """

    def on_mutation(self, event: MutationEvent) -> None: ...
