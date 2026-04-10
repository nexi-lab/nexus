"""VFS Hook contracts — context dataclasses and hook protocols.

These are inter-layer contracts used by the kernel (call sites in
``nexus_fs.py``) and implemented by service-layer
hooks.  The kernel creates context objects after each VFS operation
and passes them to ``KernelDispatch`` (injected via DI).

Two-phase dispatch model (Issue #900):
    INTERCEPT — synchronous, ordered.  Can abort (raise) or modify context.
    OBSERVE   — fire-and-forget (``FileEvent``).  Cannot abort.

All eight operations are covered:
    read / write / delete / rename / mkdir / rmdir / stat / access

Issue #625: Extracted from ``core/vfs_hooks.py``.
Issue #900: Added MkdirHookContext, RmdirHookContext, VFSMkdirHook,
            VFSRmdirHook.  Unified under KernelDispatch.
Issue #1815: Added StatHookContext, AccessHookContext, VFSStatHook,
             VFSAccessHook for permission enforcement migration.
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
class CopyHookContext:
    """Context passed through copy hooks (Issue #3329)."""

    src_path: str
    dst_path: str
    context: OperationContext | None
    zone_id: str | None = None
    agent_id: str | None = None
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


@dataclass
class StatHookContext:
    """Context for stat/is_directory permission check (Issue #1815).

    Hooks raise ``PermissionDeniedError`` to deny access.
    """

    path: str
    context: OperationContext | None
    permission: str = "TRAVERSE"  # TRAVERSE or READ
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccessHookContext:
    """Context for access permission check (Issue #1815).

    Hooks raise ``PermissionDeniedError`` to deny access.
    """

    path: str
    context: OperationContext | None
    permission: str = "TRAVERSE"  # TRAVERSE or READ
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
class VFSCopyHook(Protocol):
    """Hook that runs after a copy operation (Issue #3329)."""

    @property
    def name(self) -> str: ...

    def on_post_copy(self, ctx: CopyHookContext) -> None: ...


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


@runtime_checkable
class VFSStatHook(Protocol):
    """INTERCEPT hook for stat/is_directory permission check (Issue #1815).

    ``on_pre_stat`` is called before stat operations.  Raise
    ``PermissionDeniedError`` to deny access.
    """

    def on_pre_stat(self, ctx: StatHookContext) -> None: ...


@runtime_checkable
class VFSAccessHook(Protocol):
    """INTERCEPT hook for access permission check (Issue #1815).

    ``on_pre_access`` is called before access checks.  Raise
    ``PermissionDeniedError`` to deny access.
    """

    def on_pre_access(self, ctx: AccessHookContext) -> None: ...


@dataclass
class WriteBatchHookContext:
    """Context passed through write-batch hooks (Issue #900)."""

    items: list[tuple[Any, bool]]
    context: OperationContext | None = None
    zone_id: str | None = None
    agent_id: str | None = None
    warnings: list[OperationWarning] = field(default_factory=list)


@runtime_checkable
class VFSWriteBatchHook(Protocol):
    """Hook that runs after a batch write operation (Issue #900)."""

    @property
    def name(self) -> str: ...

    def on_post_write_batch(self, ctx: WriteBatchHookContext) -> None: ...


@dataclass
class ReadBatchHookContext:
    """Context passed through read-batch hooks (Issue #3700)."""

    items: list[tuple[Any, FileMetadata | None]]  # (path, metadata_or_None)
    context: OperationContext | None = None
    zone_id: str | None = None
    agent_id: str | None = None
    warnings: list[OperationWarning] = field(default_factory=list)


@runtime_checkable
class VFSReadBatchHook(Protocol):
    """Hook that runs after a batch read operation (Issue #3700)."""

    @property
    def name(self) -> str: ...

    def on_post_read_batch(self, ctx: ReadBatchHookContext) -> None: ...


# ---------------------------------------------------------------------------
# OBSERVE phase — observer protocol (Issue #900)
# ---------------------------------------------------------------------------


@runtime_checkable
class VFSObserver(Protocol):
    """OBSERVE-phase observer for kernel VFS mutations (fire-and-forget).

    Receives a frozen ``FileEvent`` after every successful mutation.
    Must not raise — exceptions are caught and logged by KernelDispatch.

    OBSERVE is fire-and-forget by definition. All observers run on the
    kernel's background ThreadPool (§11 Phase 3), off the syscall hot
    path. There is no other mode — ``OBSERVE_INLINE`` was deleted in
    §11 Phase 2 because inline observers were functionally identical to
    INTERCEPT POST hooks and violated dispatch-contract orthogonality.
    Observers needing sync blocking on the syscall return path belong in
    INTERCEPT POST, not OBSERVE.

    Optional class attributes:

    ``event_mask`` (default: ``ALL_FILE_EVENTS``)
        Rust-side event-type bitmask filtering to skip irrelevant observers.
    """

    def on_mutation(self, event: Any) -> None: ...


# ---------------------------------------------------------------------------
# PRE-DISPATCH phase — virtual path resolver protocol (Issue #889)
# ---------------------------------------------------------------------------


@runtime_checkable
class VFSPathResolver(Protocol):
    """PRE-DISPATCH resolver for virtual paths (Issue #889, #1665).

    Linux analogue: virtual filesystem dispatch (procfs, sysfs, devtmpfs).
    When a path is claimed by a resolver, the resolver handles the entire
    operation — the normal VFS pipeline is skipped.  Each resolver owns
    its own permission semantics.

    Single-call ``try_*`` pattern: each method returns ``None`` when the
    resolver does not claim the path ("not my path"), or the operation
    result when it does.  This eliminates the old two-phase
    ``matches()`` + ``read/write/delete()`` dispatch.

    Registered at boot via factory into KernelDispatch.  Empty resolver
    chain = no-op = zero overhead when no resolvers registered.
    """

    # Content I/O routing
    def try_read(self, path: str, *, context: Any = None) -> bytes | None: ...
    def try_write(self, path: str, content: bytes) -> dict[str, Any] | None: ...
    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# MOUNT/UNMOUNT hooks — driver lifecycle notifications (Issue #1811)
# ---------------------------------------------------------------------------


@dataclass
class MountHookContext:
    """Context passed to mount hooks when a backend is mounted."""

    mount_point: str
    backend: Any  # ObjectStoreABC


@dataclass
class UnmountHookContext:
    """Context passed to unmount hooks when a backend is unmounted."""

    mount_point: str
    backend: Any  # ObjectStoreABC


@runtime_checkable
class VFSMountHook(Protocol):
    """Hook that runs when a backend is mounted (Issue #1811).

    Linux analogue: ``file_system_type.mount()``.
    Fire-and-forget — failures are caught and logged by KernelDispatch.
    Dispatched by DriverLifecycleCoordinator via KernelDispatch.
    """

    def on_mount(self, ctx: MountHookContext) -> None: ...


@runtime_checkable
class VFSUnmountHook(Protocol):
    """Hook that runs when a backend is unmounted (Issue #1811).

    Linux analogue: ``kill_sb()``.
    Fire-and-forget — failures are caught and logged by KernelDispatch.
    """

    def on_unmount(self, ctx: UnmountHookContext) -> None: ...
