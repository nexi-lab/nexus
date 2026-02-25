"""Memory I/O handler — VFSPathResolver for memory virtual paths (#889).

Implements the ``VFSPathResolver`` protocol and is registered in
``KernelDispatch`` as a PRE-DISPATCH resolver.  When a read/write/delete
targets a memory path, this handler short-circuits the normal VFS
pipeline and handles the operation directly.

Linux analogue: procfs ``proc_reg_read()`` / ``proc_reg_write()`` —
a virtual filesystem whose file_operations bypass the block layer.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.vfs_hooks import VFSPathResolver

logger = logging.getLogger(__name__)


class MemoryIOHandler(VFSPathResolver):
    """PRE-DISPATCH resolver for memory virtual paths.

    Implements ``VFSPathResolver`` protocol:
    - ``matches(path)`` — routing predicate
    - ``read(path, ...)`` — memory read
    - ``write(path, content)`` — memory write
    - ``delete(path, ...)`` — memory delete

    Dependencies injected via constructor (no kernel imports at runtime):
    - memory_router: MemoryViewRouter (path resolution + metadata)
    - memory_provider: MemoryProvider (lazy Memory API factory)
    - path_router: PathRouter (CAS content routing)
    """

    __slots__ = ("_router", "_provider", "_path_router")

    def __init__(
        self,
        memory_router: Any,
        memory_provider: Any,
        path_router: Any,
    ) -> None:
        self._router = memory_router
        self._provider = memory_provider
        self._path_router = path_router

    # ------------------------------------------------------------------
    # VFSPathResolver protocol
    # ------------------------------------------------------------------

    def matches(self, path: str) -> bool:
        """Return True if *path* is a memory virtual path."""
        return bool(self._router.is_memory_path(path))

    def read(
        self, path: str, *, return_metadata: bool = False, context: Any = None
    ) -> bytes | dict[str, Any]:
        """Read memory via virtual path.

        Extracted from ``NexusFSCoreMixin._read_memory_path``.
        """
        memory = self._router.resolve(path)
        if not memory:
            raise NexusFileNotFoundError(f"Memory not found at path: {path}")

        # Read content from CAS — route the memory path to find the backend
        route = self._path_router.route(path, is_admin=True)
        content: bytes = route.backend.read_content(memory.content_hash, context=context)

        if return_metadata:
            return {
                "content": content,
                "etag": memory.content_hash,
                "version": 1,  # Memories don't version like files
                "modified_at": memory.created_at,
                "size": len(content),
            }
        return content

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        """Write memory via virtual path.

        Extracted from ``NexusFSCoreMixin._write_memory_path``.
        """
        memory_api = self._provider.get_or_create()
        if memory_api is None:
            raise RuntimeError("Memory API not initialized")

        # Extract memory type from path if present
        parts = [p for p in path.split("/") if p]
        memory_type = None
        if "memory" in parts:
            idx = parts.index("memory")
            if idx + 1 < len(parts):
                memory_type = parts[idx + 1]

        # Store memory with default scope='user'
        memory_id = memory_api.store(
            content=content.decode("utf-8") if isinstance(content, bytes) else content,
            scope="user",
            memory_type=memory_type,
        )

        # Get the created memory
        mem = memory_api.get(memory_id)
        if mem is None:
            raise RuntimeError(
                f"Failed to retrieve stored memory (id={memory_id}). "
                "The memory API may not be properly configured or the memory was not persisted."
            )

        return {
            "etag": mem["content_hash"],
            "version": 1,
            "modified_at": mem["created_at"],
            "size": len(content),
        }

    def delete(self, path: str, *, context: Any = None) -> None:
        """Delete memory via virtual path.

        Extracted from ``NexusFSCoreMixin._delete_memory_path``.
        """
        memory = self._router.resolve(path)
        if not memory:
            raise NexusFileNotFoundError(f"Memory not found at path: {path}")

        # Delete the memory
        self._router.delete_memory(memory.memory_id)

        # Also delete content from CAS (decrement ref count)
        route = self._path_router.route(path, is_admin=True)
        route.backend.delete_content(memory.content_hash, context=context)
