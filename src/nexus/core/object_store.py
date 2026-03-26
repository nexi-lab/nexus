"""ObjectStoreABC -- the Object Store pillar of the Four Storage Pillars.

Kernel ``file_operations`` contract (Linux analogue: ``struct file_operations``).
Exception-based errors, consistent with MetastoreABC / RecordStoreABC / CacheStoreABC.

Design:
    - ABC (not Protocol) -- concrete defaults for streaming, batch, capability flags
    - 6 abstract methods: write_content, read_content, delete_content,
      get_content_size, mkdir, rmdir
    - WriteResult returned from write operations (content_id + size)
    - Callers get raw types, errors are exceptions
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

from nexus.contracts.types import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

# Re-export WriteResult for backward compatibility — canonical home is contracts.types
__all__ = ["ObjectStoreABC", "WriteResult"]


class ObjectStoreABC(ABC):
    """ObjectStore pillar -- kernel ``file_operations`` contract.

    Linux analogue: ``struct file_operations``.
    Exception-based errors.

    Subclasses must implement the 6 abstract methods.  Streaming, batch,
    capability flags, and lifecycle have concrete defaults that work out of
    the box.
    """

    # === Identity ===

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier name (e.g. ``"local"``, ``"gcs"``, ``"s3"``)."""
        ...

    # === Content Operations (4 abstract) ===

    @abstractmethod
    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> WriteResult:
        """Write content to storage and return a ``WriteResult``.

        Args:
            content: File content as bytes.
            content_id: Target address for the content.
                CAS backends: ignored (address = hash of content).
                PAS backends: blob path where content will be stored.
            context: Operation context (optional, for auth / cross-cutting).
            offset: Byte offset for partial write (POSIX pwrite semantics).
                0 = whole-file replace (default, backward compatible).
                >0 = splice ``content`` at offset within existing content.

        Returns:
            ``WriteResult`` with ``content_id``, ``version``, and ``size``.

        Raises:
            BackendError: If write operation fails.
        """
        ...

    @abstractmethod
    def read_content(self, content_id: str, context: OperationContext | None = None) -> bytes:
        """Read content by its opaque identifier.

        Args:
            content_id: Opaque content identifier (e.g. SHA-256 hash for CAS,
                version ID for path-based backends).
            context: Operation context (optional).

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If read operation fails.
        """
        ...

    @abstractmethod
    def delete_content(self, content_id: str, context: OperationContext | None = None) -> None:
        """Delete content by identifier.

        Addressing-agnostic: CAS backends may defer actual deletion until
        garbage collection; PAS backends delete the blob at the given path.

        Args:
            content_id: Opaque content identifier.
            context: Operation context (optional).

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If delete operation fails.
        """
        ...

    @abstractmethod
    def get_content_size(self, content_id: str, context: OperationContext | None = None) -> int:
        """Get content size in bytes.

        Args:
            content_id: Opaque content identifier.
            context: Operation context (optional).

        Returns:
            Content size in bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
        """
        ...

    # === Streaming (concrete defaults) ===

    def write_stream(
        self,
        chunks: Iterator[bytes],
        content_id: str = "",
        *,
        context: OperationContext | None = None,
    ) -> WriteResult:
        """Write content from an iterator of chunks.

        Default implementation collects all chunks into memory and
        delegates to ``write_content()``.  Backends should override for
        true streaming with incremental hashing.

        Args:
            chunks: Iterator yielding byte chunks.
            content_id: Target address (see ``write_content``).
            context: Operation context (optional).

        Returns:
            ``WriteResult`` with ``content_id``, ``version``, and ``size``.
        """
        content = b"".join(chunks)
        return self.write_content(content, content_id, context=context)

    def stream_content(
        self,
        content_id: str,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> Iterator[bytes]:
        """Stream content in chunks (generator).

        Default implementation reads the full content and yields slices.
        Backends with seekable storage should override for efficiency.

        Args:
            content_id: Opaque content identifier.
            chunk_size: Size of each chunk in bytes (default: 8 KiB).
            context: Operation context (optional).

        Yields:
            Chunks of file content.
        """
        content = self.read_content(content_id, context=context)
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def stream_range(
        self,
        content_id: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> Iterator[bytes]:
        """Stream a byte range ``[start, end]`` inclusive from stored content.

        Default implementation reads full content and slices.  Backends
        with seekable storage should override for efficiency.

        Args:
            content_id: Opaque content identifier.
            start: First byte position (inclusive, 0-based).
            end: Last byte position (inclusive, 0-based).
            chunk_size: Size of each yielded chunk in bytes.
            context: Operation context (optional).

        Yields:
            Chunks covering the requested range.
        """
        content = self.read_content(content_id, context=context)
        sliced = content[start : end + 1]
        for i in range(0, len(sliced), chunk_size):
            yield sliced[i : i + chunk_size]

    # === Directory Operations (2 abstract) ===

    @abstractmethod
    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """Create a directory.

        For backends without native directory support (e.g. S3), this may
        be a no-op or create marker objects.

        Args:
            path: Directory path (relative to backend root).
            parents: Create parent directories if needed (like ``mkdir -p``).
            exist_ok: Do not raise if directory already exists.
            context: Operation context (optional).

        Raises:
            BackendError: If directory creation fails.
        """
        ...

    @abstractmethod
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """Remove a directory.

        Args:
            path: Directory path.
            recursive: Remove non-empty directory (like ``rm -rf``).
            context: Operation context (optional).

        Raises:
            BackendError: If directory removal fails.
        """
        ...

    # === Batch (concrete default) ===

    def batch_read_content(
        self,
        content_ids: list[str],
        context: OperationContext | None = None,
        *,
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes | None]:
        """Read multiple content items by their identifiers.

        Default implementation calls ``read_content()`` for each id.
        Backends should override for better performance (e.g. batch RPCs).

        Unlike ``read_content()``, missing content is indicated by
        ``None`` values in the result dict rather than raising.

        Args:
            content_ids: List of opaque content identifiers.
            context: Shared operation context (fallback).
            contexts: Per-id operation contexts mapping
                ``content_id -> OperationContext``.

        Returns:
            Dict mapping ``content_id -> bytes | None``.
        """
        result: dict[str, bytes | None] = {}
        for content_id in content_ids:
            ctx = contexts.get(content_id, context) if contexts else context
            try:
                result[content_id] = self.read_content(content_id, context=ctx)
            except Exception:
                result[content_id] = None
        return result

    # === Capability Flags (concrete defaults, all False) ===

    @property
    def user_scoped(self) -> bool:
        """Whether this backend requires per-user credentials (OAuth-based)."""
        return False

    @property
    def has_token_manager(self) -> bool:
        """Whether this backend manages OAuth tokens."""
        return False

    @property
    def supports_rename(self) -> bool:
        """Whether this backend supports direct file rename/move."""
        return False

    @property
    def supports_parallel_mmap_read(self) -> bool:
        """Whether this backend supports Rust-accelerated parallel mmap reads."""
        return False

    # === Lifecycle ===

    def close(self) -> None:  # noqa: B027
        """Release resources.  Consistent with MetastoreABC.close() / CacheStoreABC.close()."""
