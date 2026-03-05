"""VFS Hook contracts — context dataclasses and hook protocols.

These are inter-layer contracts used by the kernel (call sites in
``nexus_fs.py``) and implemented by service-layer
hooks.  The kernel creates context objects after each VFS operation
and passes them to ``KernelDispatch`` (injected via DI).

Two-phase dispatch model (Issue #900):
    INTERCEPT — synchronous, ordered.  Can abort (raise) or modify context.
    OBSERVE   — fire-and-forget (``FileEvent``).  Cannot abort.

All six operations are covered:
    read / write / delete / rename / mkdir / rmdir

Issue #625: Extracted from ``core/vfs_hooks.py``.
Issue #900: Added MkdirHookContext, RmdirHookContext, VFSMkdirHook,
            VFSRmdirHook.  Unified under KernelDispatch.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from nexus.contracts.metadata import FileMetadata
from nexus.contracts.operation_result import OperationWarning
from nexus.contracts.types import OperationContext

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
# Process hook contexts — agent runtime (Issue #2761)
# ---------------------------------------------------------------------------


@dataclass
class ProcessSpawnHookContext:
    """Context passed through process spawn hooks (Issue #2761)."""

    agent_id: str
    zone_id: str
    pid: str
    parent_pid: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[OperationWarning] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessTerminateHookContext:
    """Context passed through process terminate hooks (Issue #2761)."""

    pid: str
    agent_id: str
    zone_id: str
    reason: str = "terminated"
    exit_code: int = 0
    warnings: list[OperationWarning] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Process hook protocol — agent runtime (Issue #2761)
# ---------------------------------------------------------------------------


@runtime_checkable
class VFSProcessHook(Protocol):
    """Hook for agent process lifecycle events (Issue #2761)."""

    @property
    def name(self) -> str: ...

    def on_pre_proc_spawn(self, ctx: ProcessSpawnHookContext) -> None:
        """Called before a process is spawned. May raise to abort."""
        ...

    def on_post_proc_spawn(self, ctx: ProcessSpawnHookContext) -> None:
        """Called after a process is spawned."""
        ...

    def on_post_proc_terminate(self, ctx: ProcessTerminateHookContext) -> None:
        """Called after a process is terminated."""
        ...


@dataclass
class WriteBatchHookContext:
    """Context passed through write-batch hooks (Issue #900)."""

    items: list[tuple[Any, bool]]
    zone_id: str | None = None
    agent_id: str | None = None
    warnings: list[OperationWarning] = field(default_factory=list)


@runtime_checkable
class VFSWriteBatchHook(Protocol):
    """Hook that runs after a batch write operation (Issue #900)."""

    @property
    def name(self) -> str: ...

    def on_post_write_batch(self, ctx: WriteBatchHookContext) -> None: ...


# ---------------------------------------------------------------------------
# OBSERVE phase — observer protocol (Issue #900)
# ---------------------------------------------------------------------------


@runtime_checkable
class VFSObserver(Protocol):
    """OBSERVE-phase observer for kernel VFS mutations (fire-and-forget).

    Receives a frozen ``FileEvent`` after every successful mutation.
    Must not raise — exceptions are caught and logged by KernelDispatch.
    """

    def on_mutation(self, event: Any) -> None: ...


# ---------------------------------------------------------------------------
# PRE-DISPATCH phase — virtual path resolver protocol (Issue #889)
# ---------------------------------------------------------------------------


@runtime_checkable
class VFSPathResolver(Protocol):
    """PRE-DISPATCH resolver for virtual paths (Issue #889).

    Linux analogue: virtual filesystem dispatch (procfs, sysfs, devtmpfs).
    When read()/write()/delete() is called on a path claimed by a resolver,
    the resolver handles the entire operation — the normal VFS pipeline is
    skipped.  Each resolver owns its own permission semantics.

    Registered at boot via factory into KernelDispatch.  Empty resolver
    chain = no-op = zero overhead when no resolvers registered.
    """

    def matches(self, path: str) -> bool: ...
    def read(
        self, path: str, *, return_metadata: bool = False, context: Any = None
    ) -> bytes | dict: ...
    def write(self, path: str, content: bytes) -> dict[str, Any]: ...
    def delete(self, path: str, *, context: Any = None) -> None: ...
