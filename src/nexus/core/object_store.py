"""ObjectStoreABC -- the Object Store pillar of the Four Storage Pillars.

Kernel ``file_operations`` contract (Linux analogue: ``struct file_operations``).
Exception-based errors, consistent with MetastoreABC / RecordStoreABC / CacheStoreABC.

Design:
    - ABC (not Protocol) -- concrete defaults for streaming, batch, capability flags
    - 6 abstract methods: write_content, read_content, delete_content,
      get_content_size, mkdir, rmdir
    - WriteResult returned from write operations (content_hash + size)
    - No HandlerResponse -- callers get raw types, errors are exceptions
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_hash(content_hash: str) -> None:
    """Validate that a content hash is a well-formed SHA-256 hex string.

    Args:
        content_hash: Value to validate

    Raises:
        ValueError: If content_hash is not a 64-character lowercase hex string
    """
    if not _HASH_PATTERN.match(content_hash):
        raise ValueError(
            f"Invalid SHA-256 content hash: {content_hash!r} "
            f"(expected 64-character lowercase hex string)"
        )


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Result of a content write operation.

    Attributes:
        content_hash: SHA-256 hex digest of the written content.
        size: Content size in bytes (0 = unknown / not tracked).
    """

    content_hash: str
    size: int = 0


class ObjectStoreABC(ABC):
    """ObjectStore pillar -- kernel ``file_operations`` contract.

    Linux analogue: ``struct file_operations``.
    Exception-based errors (no HandlerResponse).

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

    # === CAS Content Operations (4 abstract) ===

    @abstractmethod
    def write_content(self, content: bytes, context: OperationContext | None = None) -> WriteResult:
        """Write content to storage and return a ``WriteResult``.

        If content already exists (same hash), deduplication is handled
        internally.

        Args:
            content: File content as bytes.
            context: Operation context (optional, for user-scoped backends).

        Returns:
            ``WriteResult`` with ``content_hash`` and ``size``.

        Raises:
            BackendError: If write operation fails.
        """
        ...

    @abstractmethod
    def read_content(self, content_hash: str, context: OperationContext | None = None) -> bytes:
        """Read content by its hash.

        Args:
            content_hash: SHA-256 hash as hex string.
            context: Operation context (optional).

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If read operation fails.
        """
        ...

    @abstractmethod
    def delete_content(self, content_hash: str, context: OperationContext | None = None) -> None:
        """Delete content by hash.

        Decrements reference count. Only deletes actual data when the
        reference count reaches zero.

        Args:
            content_hash: SHA-256 hash as hex string.
            context: Operation context (optional).

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If delete operation fails.
        """
        ...

    @abstractmethod
    def get_content_size(self, content_hash: str, context: OperationContext | None = None) -> int:
        """Get content size in bytes.

        Args:
            content_hash: SHA-256 hash as hex string.
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
        context: OperationContext | None = None,
    ) -> WriteResult:
        """Write content from an iterator of chunks.

        Default implementation collects all chunks into memory and
        delegates to ``write_content()``.  Backends should override for
        true streaming with incremental hashing.

        Args:
            chunks: Iterator yielding byte chunks.
            context: Operation context (optional).

        Returns:
            ``WriteResult`` with ``content_hash`` and ``size``.
        """
        content = b"".join(chunks)
        return self.write_content(content, context=context)

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> Iterator[bytes]:
        """Stream content by hash in chunks (generator).

        Default implementation reads the full content and yields slices.
        Backends with seekable storage should override for efficiency.

        Args:
            content_hash: SHA-256 hash as hex string.
            chunk_size: Size of each chunk in bytes (default: 8 KiB).
            context: Operation context (optional).

        Yields:
            Chunks of file content.
        """
        content = self.read_content(content_hash, context=context)
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def stream_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> Iterator[bytes]:
        """Stream a byte range ``[start, end]`` inclusive from stored content.

        Default implementation reads full content and slices.  Backends
        with seekable storage should override for efficiency.

        Args:
            content_hash: Content identifier (hash).
            start: First byte position (inclusive, 0-based).
            end: Last byte position (inclusive, 0-based).
            chunk_size: Size of each yielded chunk in bytes.
            context: Operation context (optional).

        Yields:
            Chunks covering the requested range.
        """
        content = self.read_content(content_hash, context=context)
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
        content_hashes: list[str],
        context: OperationContext | None = None,
        *,
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes | None]:
        """Read multiple content items by their hashes.

        Default implementation calls ``read_content()`` for each hash.
        Backends should override for better performance (e.g. batch RPCs).

        Unlike ``read_content()``, missing content is indicated by
        ``None`` values in the result dict rather than raising.

        Args:
            content_hashes: List of SHA-256 hashes.
            context: Shared operation context (fallback).
            contexts: Per-hash operation contexts mapping
                ``content_hash -> OperationContext``.

        Returns:
            Dict mapping ``content_hash -> bytes | None``.
        """
        result: dict[str, bytes | None] = {}
        for content_hash in content_hashes:
            ctx = contexts.get(content_hash, context) if contexts else context
            try:
                result[content_hash] = self.read_content(content_hash, context=ctx)
            except Exception:
                result[content_hash] = None
        return result

    # === Capability Flags (concrete defaults, all False) ===

    @property
    def user_scoped(self) -> bool:
        """Whether this backend requires per-user credentials (OAuth-based)."""
        return False

    @property
    def has_virtual_filesystem(self) -> bool:
        """Whether this backend uses a virtual filesystem (e.g. API-backed)."""
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

    @property
    def is_passthrough(self) -> bool:
        """Whether this backend is a PassthroughBackend for same-box mode."""
        return False

    # === Lifecycle ===

    def close(self) -> None:  # noqa: B027
        """Release resources.  Consistent with MetastoreABC.close() / CacheStoreABC.close()."""
