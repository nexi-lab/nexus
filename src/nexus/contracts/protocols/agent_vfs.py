"""Minimal VFS protocol for the agent runtime (Issue #2761).

Defines two narrow protocols that decouple the agent runtime from
concrete NexusFS, allowing ProcessManager/ToolDispatcher/SessionStore
to operate over any VFS-like object (NexusFS, ScopedFilesystem, RPC client).

Design principle: keep the surface area minimal — only the methods the
agent runtime actually calls. NexusFS already implements both protocols
via duck typing; no changes to NexusFS are needed.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentVFSProtocol(Protocol):
    """Minimal VFS surface used by the agent runtime.

    NexusFS, ScopedFilesystem, and RPC clients all satisfy this protocol.
    """

    def sys_read(self, path: str, *, context: Any = None) -> bytes | dict[str, Any]: ...

    def sys_write(self, path: str, buf: bytes, *, context: Any = None) -> int | dict[str, Any]: ...

    def sys_access(self, path: str, *, context: Any = None) -> bool: ...

    def sys_readdir(
        self, path: str, *, recursive: bool = False, context: Any = None
    ) -> list[Any]: ...


@runtime_checkable
class AgentSearchProtocol(Protocol):
    """Optional search surface (grep/glob) used by ToolDispatcher.

    Separate from VFS because search is a brick-level service, not a
    kernel syscall. Graceful degradation: ToolDispatcher works without
    this (falls back to sys_readdir).
    """

    def grep(
        self,
        *,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 50,
    ) -> list[dict[str, Any]]: ...

    def glob(self, pattern: str, path: str = "/") -> list[str]: ...
