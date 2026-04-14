"""Filesystem Protocol — kernel syscall contract.

Linux analogy: the syscall table (``read``, ``write``, ``open``, ``unlink``,
``stat``, ``readdir``, …). This is the **user-facing entry point** into the
kernel — every RPC/CLI call resolves to one of these methods.

Beneath this layer, NexusFS dispatches to ObjectStoreABC backends
(``file_operations``), MetastoreABC (inode table), and hook pipelines.

Service-layer operations (workspace, memory, sandbox) have their own
protocols in ``services/protocols/`` and are NOT part of the kernel contract.

Two tiers:
  Tier 1: ``sys_`` syscalls — implementors MUST provide.
          Named after POSIX syscalls where a classic name exists;
          historical baggage replaced with better names for our context.
  Tier 2: Convenience methods — compose syscalls.
          User-space utilities (like libc/coreutils).
          Implementations live in NexusFS directly.
"""

import builtins
from typing import Any, Protocol, runtime_checkable

from nexus.contracts.types import OperationContext


@runtime_checkable
class NexusFilesystem(Protocol):
    """Kernel syscall contract — Linux VFS-aligned.

    All filesystem modes (Standalone, Remote, Federation) must implement
    this interface structurally. Service-layer concerns (workspace, memory,
    sandbox) are deliberately excluded — they belong to their respective
    service protocols, not the kernel.

    Error pattern: mutations raise, queries return False/None, stat returns None if not found.
    """

    # ── Service Registry ──────────────────────────────────────────

    def service(self, name: str) -> Any | None: ...

    # ── Tier 1: Syscalls ──────────────────────────────────────────
    #
    # Content I/O — sys_read(2), sys_write(2)
    # Metadata I/O — sys_stat(2), sys_setattr (chmod/chown/utimensat)
    # Namespace — sys_unlink(2), sys_rename(2), sys_copy
    # Directory — sys_readdir(3)
    # Locking — sys_lock, sys_unlock
    # Watch — sys_watch

    # ── Content I/O ────────────────────────────────────────────────

    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> bytes: ...

    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    # ── Metadata I/O ───────────────────────────────────────────────

    def sys_stat(
        self, path: str, *, context: OperationContext | None = None
    ) -> dict[str, Any] | None: ...

    def sys_setattr(
        self, path: str, *, context: OperationContext | None = None, **attrs: Any
    ) -> dict[str, Any]: ...

    # ── Namespace ──────────────────────────────────────────────────

    def sys_unlink(
        self, path: str, *, recursive: bool = False, context: OperationContext | None = None
    ) -> dict[str, Any]: ...

    def sys_rename(
        self,
        old_path: str,
        new_path: str,
        *,
        force: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    def sys_copy(
        self, src_path: str, dst_path: str, *, context: OperationContext | None = None
    ) -> dict[str, Any]: ...

    # ── Directory ──────────────────────────────────────────────────

    def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        *,
        context: OperationContext | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | Any: ...

    # ── Locking ────────────────────────────────────────────────────

    def sys_lock(
        self,
        path: str,
        mode: str = "exclusive",
        ttl: float = 30.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,
    ) -> str | None: ...

    def sys_unlock(
        self, path: str, lock_id: str, *, context: "OperationContext | None" = None
    ) -> bool: ...

    # ── Watch ──────────────────────────────────────────────────────

    async def sys_watch(
        self,
        path: str,
        timeout: float = 30.0,
        *,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None: ...

    # ── Tier 2: Convenience ───────────────────────────────────────

    def mkdir(
        self,
        path: str,
        parents: bool = True,
        exist_ok: bool = True,
        *,
        context: OperationContext | None = None,
    ) -> None: ...

    def rmdir(
        self,
        path: str,
        recursive: bool = True,
        *,
        context: OperationContext | None = None,
    ) -> None: ...

    def access(self, path: str, *, context: OperationContext | None = None) -> bool: ...

    def is_directory(self, path: str, *, context: OperationContext | None = None) -> bool: ...

    def lock(
        self,
        path: str,
        mode: str = "exclusive",
        timeout: float = 30.0,
        ttl: float = 60.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,
    ) -> str | None: ...

    def unlock(
        self, lock_id: str, path: str, *, context: "OperationContext | None" = None
    ) -> bool: ...

    def locked(
        self,
        path: str,
        mode: str = "exclusive",
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,
    ) -> Any: ...

    def read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]: ...

    def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        consistency: str = "sc",
    ) -> dict[str, Any]: ...

    def append(
        self,
        path: str,
        content: bytes | str,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    def edit(
        self,
        path: str,
        edits: builtins.list[tuple[str, str]] | builtins.list[dict[str, Any]] | builtins.list[Any],
        *,
        context: OperationContext | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]: ...

    def write_batch(
        self,
        files: builtins.list[tuple[str, bytes]],
        *,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]: ...

    def read_batch(
        self,
        paths: builtins.list[str],
        *,
        partial: bool = False,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]: ...

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]: ...

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
    ) -> builtins.list[dict[str, Any]]: ...

    # ── System Info + Lifecycle ────────────────────────────────────

    def get_top_level_mounts(self, context: Any = None) -> builtins.list[str]: ...

    def close(self) -> None: ...
