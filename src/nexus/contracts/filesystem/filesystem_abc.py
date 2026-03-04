"""Filesystem ABC — kernel syscall contract.

Linux analogy: the syscall table (``read``, ``write``, ``open``, ``unlink``,
``stat``, ``readdir``, …). This is the **user-facing entry point** into the
kernel — every RPC/CLI call resolves to one of these methods.

Beneath this layer, NexusFS dispatches to ObjectStoreABC backends
(``file_operations``), MetastoreABC (inode table), and hook pipelines.

Service-layer operations (workspace, memory, sandbox) have their own
protocols in ``services/protocols/`` and are NOT part of the kernel contract.

Two tiers:
  Tier 1: Abstract ``sys_`` syscalls — implementors MUST override.
          Named after POSIX syscalls where a classic name exists;
          historical baggage replaced with better names for our context.
  Tier 2: Convenience methods — concrete, compose syscalls.
          User-space utilities (like libc/coreutils). NOT abstract.
          Overridable for optimization.
"""

import builtins
from abc import ABC, abstractmethod
from typing import Any

from nexus.contracts.types import OperationContext


class NexusFilesystemABC(ABC):
    """Kernel syscall contract — Linux VFS-aligned.

    All filesystem modes (Standalone, Remote, Federation) must implement
    this interface. Service-layer concerns (workspace, memory, sandbox)
    are deliberately excluded — they belong to their respective service
    protocols, not the kernel.
    """

    # ── Tier 1: Abstract Syscalls ──────────────────────────────────
    #
    # Content I/O — sys_read(2), sys_write(2)
    # Metadata I/O — sys_stat(2), sys_setattr (chmod/chown/utimensat)
    # Namespace — sys_unlink(2), sys_rename(2)
    # Directory — sys_mkdir(2), sys_rmdir(2), sys_readdir(3)
    # Query — sys_access(2), sys_is_directory
    # System — get_top_level_mounts, close

    # ── Content I/O ────────────────────────────────────────────────

    @abstractmethod
    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read file content (POSIX pread(2)).

        Args:
            path: Virtual file path.
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Operation context (auth, zone, etc.).

        Returns:
            File content as bytes.
        """
        ...

    @abstractmethod
    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> int:
        """Write content to a file (POSIX pwrite(2)).

        Content-only primitive. CAS and locking are driver/application
        concerns — not kernel. Metadata update is a kernel side effect
        (Phase A); Phase B will separate into sys_write + sys_setattr.

        Args:
            path: Virtual file path.
            buf: File content as bytes or str.
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset to start writing at.
            context: Operation context.

        Returns:
            Number of bytes written.
        """
        ...

    # ── Metadata I/O ───────────────────────────────────────────────

    @abstractmethod
    def sys_stat(self, path: str, context: Any = None) -> dict[str, Any] | None:
        """Read all file metadata (POSIX stat(2)).

        Returns:
            Dict of metadata fields, or None if file not found.
        """
        ...

    @abstractmethod
    def sys_setattr(self, path: str, context: Any = None, **attrs: Any) -> dict[str, Any]:
        """Update file metadata attributes (chmod/chown/utimensat analog).

        Linux has separate chmod(2), chown(2), utimensat(2) — we combine
        into one call (better for VFS).

        Args:
            path: Virtual file path.
            context: Operation context.
            **attrs: Metadata attributes to update.

        Returns:
            Dict with path and list of updated attributes.
        """
        ...

    # ── Namespace ──────────────────────────────────────────────────

    @abstractmethod
    def sys_unlink(self, path: str, context: Any = None) -> dict[str, Any]:
        """Remove a directory entry (POSIX unlink(2)).

        NOT "delete" — unlink is precise: removes directory entry,
        CAS refcount decrements. Content freed only when refcount=0.
        """
        ...

    @abstractmethod
    def sys_rename(self, old_path: str, new_path: str, context: Any = None) -> dict[str, Any]:
        """Rename/move a file (POSIX rename(2))."""
        ...

    # ── Directory ──────────────────────────────────────────────────

    @abstractmethod
    def sys_mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: Any = None,
    ) -> None:
        """Create a directory (POSIX mkdir(2))."""
        ...

    @abstractmethod
    def sys_rmdir(self, path: str, recursive: bool = False, context: Any = None) -> None:
        """Remove a directory (POSIX rmdir(2))."""
        ...

    @abstractmethod
    def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List directory entries (POSIX readdir(3)).

        Replaces ``list()`` — readdir is the POSIX name.
        """
        ...

    # ── Query ──────────────────────────────────────────────────────

    @abstractmethod
    def sys_access(self, path: str, context: Any = None) -> bool:
        """Check if a file exists (POSIX access(2)).

        Simplified to existence check. Permission checks handled
        separately by ReBAC service.
        """
        ...

    @abstractmethod
    def sys_is_directory(self, path: str, context: OperationContext | None = None) -> bool:
        """Check if path is a directory.

        Linux uses stat(2) + S_ISDIR macro — we provide direct check
        for convenience.
        """
        ...

    # ── System Info + Lifecycle ────────────────────────────────────

    @abstractmethod
    def get_top_level_mounts(self) -> builtins.list[str]:
        """Get list of top-level mount names."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the filesystem and release resources."""
        ...

    def __enter__(self) -> "NexusFilesystemABC":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    # ── Tier 2: Convenience Methods (user-space utilities) ─────────
    #
    # NOT abstract. Compose from syscalls. Overridable for optimization.
    # Like libc/coreutils built on top of syscalls.
    #
    # Two halves (see syscall-design.md §3):
    #   VFS half  — POSIX-aligned: read, write, stat, mkdir, unlink, append, edit
    #   HDFS half — driver-level content access: read_content, write_content

    # ── VFS Half ──────────────────────────────────────────────────

    def read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read with optional metadata (VFS convenience).

        Composes sys_stat + sys_read. POSIX pread semantics.
        Override in NexusFS for parsed-content support.

        Args:
            path: Virtual file path.
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Operation context.
            return_metadata: If True, return dict with content + metadata.

        Returns:
            bytes if return_metadata=False, else dict with content + metadata.
        """
        content = self.sys_read(path, count=count, offset=offset, context=context)
        if not return_metadata:
            return content
        meta = self.sys_stat(path, context=context)
        result: dict[str, Any] = {"content": content}
        if meta:
            result.update(
                {
                    "etag": meta.get("etag"),
                    "version": meta.get("version"),
                    "modified_at": meta.get("modified_at"),
                    "size": meta.get("size"),
                }
            )
        return result

    def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Write with metadata update (VFS convenience).

        Composes sys_write + sys_setattr. POSIX pwrite + metadata update.
        Override in NexusFS for driver-specific params (CAS/lock).

        Args:
            path: Virtual file path.
            buf: File content as bytes or str.
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset to start writing at.
            context: Operation context.

        Returns:
            Dict with metadata (etag, version, modified_at, size).
        """
        self.sys_write(path, buf, count=count, offset=offset, context=context)
        meta = self.sys_stat(path, context=context)
        return meta or {}

    def append(
        self,
        path: str,
        content: bytes | str,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Append content to a file (like shell >>).

        User-space: read + write.
        """
        try:
            existing = self.sys_read(path, context=context)
        except FileNotFoundError:
            existing = b""
        if isinstance(content, str):
            content = content.encode("utf-8")
        if isinstance(existing, str):
            existing = existing.encode("utf-8")
        return self.write(path, existing + content, context=context)

    def edit(
        self,
        path: str,
        edits: builtins.list[tuple[str, str]] | builtins.list[dict[str, Any]] | builtins.list[Any],
        *,
        context: OperationContext | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Apply surgical search/replace edits to a file.

        User-space: read + modify + write.
        Override in NexusFS (requires EditEngine).
        """
        raise NotImplementedError("Override in NexusFS (requires EditEngine)")

    def write_batch(
        self,
        files: builtins.list[tuple[str, bytes]],
        *,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Write multiple files. Default: N × write()."""
        return [self.write(p, c, context=context) for p, c in files]
