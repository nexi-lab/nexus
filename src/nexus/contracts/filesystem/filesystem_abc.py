"""Filesystem ABC — kernel syscall contract.

Linux analogy: the syscall table (``read``, ``write``, ``open``, ``unlink``,
``stat``, ``getdents``, …). This is the **user-facing entry point** into the
kernel — every RPC/CLI call resolves to one of these methods.

Beneath this layer, NexusFS dispatches to ObjectStoreABC backends
(``file_operations``), MetastoreABC (inode table), and hook pipelines.

Service-layer operations (workspace, memory, sandbox) have their own
protocols in ``services/protocols/`` and are NOT part of the kernel contract.
"""

import builtins
from abc import ABC, abstractmethod
from typing import Any

from nexus.contracts.types import OperationContext


class NexusFilesystemABC(ABC):
    """Kernel syscall contract — file I/O, discovery, directories, lifecycle.

    All filesystem modes (Standalone, Remote, Federation) must implement
    this interface. Service-layer concerns (workspace, memory, sandbox)
    are deliberately excluded — they belong to their respective service
    protocols, not the kernel.
    """

    # ── File I/O (sys_read / sys_write / sys_unlink / sys_rename) ────────

    @abstractmethod
    def read(
        self, path: str, context: Any = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Read file content."""
        ...

    @abstractmethod
    def write(
        self,
        path: str,
        content: bytes,
        context: Any = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        lock: bool = False,
        lock_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Write content to a file."""
        ...

    @abstractmethod
    def write_batch(
        self, files: builtins.list[tuple[str, bytes]], context: Any = None
    ) -> builtins.list[dict[str, Any]]:
        """Write multiple files in a single transaction."""
        ...

    @abstractmethod
    def append(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Append content to an existing file or create a new one."""
        ...

    @abstractmethod
    def edit(
        self,
        path: str,
        edits: builtins.list[tuple[str, str]] | builtins.list[dict[str, Any]] | builtins.list[Any],
        context: Any = None,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Apply surgical search/replace edits to a file."""
        ...

    @abstractmethod
    def delete(self, path: str, context: Any = None) -> dict[str, Any]:
        """Delete a file."""
        ...

    @abstractmethod
    def rename(self, old_path: str, new_path: str, context: Any = None) -> dict[str, Any]:
        """Rename/move a file (metadata-only operation)."""
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if a file exists."""
        ...

    # ── Discovery (sys_getdents / userspace search) ──────────────────────

    @abstractmethod
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory."""
        ...

    @abstractmethod
    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern."""
        ...

    @abstractmethod
    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns."""
        ...

    # ── Directory Operations (sys_mkdir / sys_rmdir / sys_stat) ──────────

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        ...

    @abstractmethod
    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory."""
        ...

    @abstractmethod
    def is_directory(self, path: str, context: OperationContext | None = None) -> bool:
        """Check if path is a directory."""
        ...

    @abstractmethod
    def get_top_level_mounts(self) -> builtins.list[str]:
        """Get list of top-level mount names."""
        ...

    # ── Lifecycle (close / context manager) ──────────────────────────────

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
