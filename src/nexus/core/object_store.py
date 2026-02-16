"""ObjectStoreABC — the Object Store pillar of the Four Storage Pillars.

Provides a content-addressed blob storage Protocol (CAS ops only).
The BackendObjectStore adapter translates Backend → ObjectStoreABC.

Design:
    - Protocol (not ABC) — matches IPCStorageDriver pattern
    - CAS ops only — no directory ops (mkdir/rmdir/list_dir/is_directory)
    - Clean return types — raises exceptions on failure (no HandlerResponse)
    - Sync methods — Backend is sync
    - No capability flags — those stay on Backend
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.permissions import OperationContext


@runtime_checkable
class ObjectStoreABC(Protocol):
    """The ObjectStore pillar — content-addressed blob storage.

    All methods use clean types: str, bytes, bool, int.
    Errors are raised as exceptions (NexusFileNotFoundError, BackendError).
    """

    @property
    def name(self) -> str:
        """Backend identifier name."""
        ...

    def write(self, content: bytes) -> str:
        """Write content, return content hash (SHA-256 hex).

        If content already exists, deduplication is handled internally.

        Args:
            content: File content as bytes

        Returns:
            Content hash (SHA-256 hex string)

        Raises:
            BackendError: If write operation fails
        """
        ...

    def read(self, content_hash: str) -> bytes:
        """Read content by hash.

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If read operation fails
        """
        ...

    def delete(self, content_hash: str) -> None:
        """Delete content by hash.

        Args:
            content_hash: SHA-256 hash as hex string

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If delete operation fails
        """
        ...

    def exists(self, content_hash: str) -> bool:
        """Check if content exists.

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            True if content exists, False otherwise
        """
        ...

    def size(self, content_hash: str) -> int:
        """Get content size in bytes.

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            Content size in bytes

        Raises:
            NexusFileNotFoundError: If content doesn't exist
        """
        ...

    def batch_read(self, content_hashes: list[str]) -> dict[str, bytes | None]:
        """Read multiple content items by hash.

        Args:
            content_hashes: List of SHA-256 hashes

        Returns:
            Dict mapping hash → content bytes (None for missing items)
        """
        ...


class BackendObjectStore:
    """Adapts a Backend instance to the ObjectStoreABC interface.

    Translates HandlerResponse[T] → T (raises on failure via .unwrap()).
    """

    def __init__(
        self,
        backend: Backend,
        context: OperationContext | None = None,
    ) -> None:
        self._backend = backend
        self._context = context

    @property
    def name(self) -> str:
        return self._backend.name

    def write(self, content: bytes) -> str:
        return self._backend.write_content(content, context=self._context).unwrap()

    def read(self, content_hash: str) -> bytes:
        return self._backend.read_content(content_hash, context=self._context).unwrap()

    def delete(self, content_hash: str) -> None:
        self._backend.delete_content(content_hash, context=self._context).unwrap()

    def exists(self, content_hash: str) -> bool:
        return self._backend.content_exists(content_hash, context=self._context).unwrap()

    def size(self, content_hash: str) -> int:
        return self._backend.get_content_size(content_hash, context=self._context).unwrap()

    def batch_read(self, content_hashes: list[str]) -> dict[str, bytes | None]:
        return self._backend.batch_read_content(content_hashes, context=self._context)
