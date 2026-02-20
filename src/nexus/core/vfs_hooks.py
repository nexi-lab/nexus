"""VFS Hook Pipeline — pre/post hooks for read and write operations.

Extracts side-effect logic (caching, ReBAC, observers, parsing, tracking)
from the read()/write() hot paths into a composable hook pipeline.

Hooks are registered at init time and invoked in order.  Each hook receives
a typed context dict and can modify it or accumulate warnings.

Architecture reference: NEXUS-LEGO-ARCHITECTURE.md §4.3

Lifecycle:
    read:   pre_read  → [kernel: validate → route → metadata → backend.read] → post_read
    write:  pre_write → [kernel: validate → route → backend.write → metadata.put] → post_write
    delete: pre_delete → [kernel: validate → route → metadata.delete] → post_delete
    rename: pre_rename → [kernel: validate → route → metadata.rename] → post_rename
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.core.operation_result import OperationWarning

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.metadata import FileMetadata


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
    """Hook that runs before or after a read operation."""

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
    """Hook that runs before or after a write operation."""

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


# ---------------------------------------------------------------------------
# Pipeline — aggregates hooks and dispatches in order
# ---------------------------------------------------------------------------


class VFSHookPipeline:
    """Aggregates and dispatches VFS hooks in registration order.

    Hooks are registered at init time (not discovered at runtime).
    Each hook failure is caught, logged, and added as a warning —
    the core operation is never aborted by a hook failure.

    Usage:
        pipeline = VFSHookPipeline()
        pipeline.register_read_hook(DynamicViewerHook(...))
        pipeline.register_read_hook(ReadTrackingHook(...))
        pipeline.register_write_hook(TigerCacheHook(...))
        pipeline.register_write_hook(ObserverHook(...))

        # In kernel read():
        ctx = ReadHookContext(path=path, context=context, content=content)
        pipeline.run_post_read(ctx)
        # ctx.content may be filtered, ctx.warnings may have entries
    """

    def __init__(self) -> None:
        self._read_hooks: list[VFSReadHook] = []
        self._write_hooks: list[VFSWriteHook] = []
        self._delete_hooks: list[VFSDeleteHook] = []
        self._rename_hooks: list[VFSRenameHook] = []

    # --- Registration ---

    def register_read_hook(self, hook: VFSReadHook) -> None:
        self._read_hooks.append(hook)

    def register_write_hook(self, hook: VFSWriteHook) -> None:
        self._write_hooks.append(hook)

    def register_delete_hook(self, hook: VFSDeleteHook) -> None:
        self._delete_hooks.append(hook)

    def register_rename_hook(self, hook: VFSRenameHook) -> None:
        self._rename_hooks.append(hook)

    # --- Dispatch ---

    def run_post_read(self, ctx: ReadHookContext) -> None:
        """Run all registered post-read hooks in order."""
        for hook in self._read_hooks:
            try:
                hook.on_post_read(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_read hook failed: {exc}",
                    )
                )

    def run_post_write(self, ctx: WriteHookContext) -> None:
        """Run all registered post-write hooks in order."""
        for hook in self._write_hooks:
            try:
                hook.on_post_write(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_write hook failed: {exc}",
                    )
                )

    def run_post_delete(self, ctx: DeleteHookContext) -> None:
        """Run all registered post-delete hooks in order."""
        for hook in self._delete_hooks:
            try:
                hook.on_post_delete(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_delete hook failed: {exc}",
                    )
                )

    def run_post_rename(self, ctx: RenameHookContext) -> None:
        """Run all registered post-rename hooks in order."""
        for hook in self._rename_hooks:
            try:
                hook.on_post_rename(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_rename hook failed: {exc}",
                    )
                )

    @property
    def read_hook_count(self) -> int:
        return len(self._read_hooks)

    @property
    def write_hook_count(self) -> int:
        return len(self._write_hooks)

    @property
    def delete_hook_count(self) -> int:
        return len(self._delete_hooks)

    @property
    def rename_hook_count(self) -> int:
        return len(self._rename_hooks)
