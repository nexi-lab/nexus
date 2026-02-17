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

import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nexus.core.permissions import OperationContext

if TYPE_CHECKING:
    from nexus.backends.backend import Backend

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

    def batch_read(
        self,
        content_hashes: list[str],
        *,
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes | None]:
        """Read multiple content items by hash.

        Args:
            content_hashes: List of SHA-256 hashes
            contexts: Per-hash operation contexts for path-based backends.
                     Maps content_hash -> OperationContext with backend_path.

        Returns:
            Dict mapping hash → content bytes (None for missing items)
        """
        ...

class BackendObjectStore:
    """Adapts a Backend instance to the ObjectStoreABC interface.

    Translates HandlerResponse[T] → T (raises on failure via .unwrap()).
    Validates content hashes at the boundary before delegating to Backend.
    """

    def __init__(
        self,
        backend: "Backend",
        context: OperationContext | None = None,
    ) -> None:
        self._backend = backend
        self._context = context

    @property
    def backend(self) -> "Backend":
        """The underlying Backend instance (read-only)."""
        return self._backend

    @property
    def name(self) -> str:
        return self._backend.name

    def __repr__(self) -> str:
        ctx = f", context={self._context!r}" if self._context is not None else ""
        return f"BackendObjectStore(backend={self._backend.name!r}{ctx})"

    def write(self, content: bytes) -> str:
        return self._backend.write_content(content, context=self._context).unwrap()

    def read(self, content_hash: str) -> bytes:
        _validate_hash(content_hash)
        return self._backend.read_content(content_hash, context=self._context).unwrap()

    def delete(self, content_hash: str) -> None:
        _validate_hash(content_hash)
        self._backend.delete_content(content_hash, context=self._context).unwrap()

    def exists(self, content_hash: str) -> bool:
        _validate_hash(content_hash)
        return self._backend.content_exists(content_hash, context=self._context).unwrap()

    def size(self, content_hash: str) -> int:
        _validate_hash(content_hash)
        return self._backend.get_content_size(content_hash, context=self._context).unwrap()

    def batch_read(
        self,
        content_hashes: list[str],
        *,
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes | None]:
        for h in content_hashes:
            _validate_hash(h)
        try:
            return self._backend.batch_read_content(
                content_hashes, context=self._context, contexts=contexts
            )
        except Exception:
            # Re-raise domain exceptions (NexusFileNotFoundError, BackendError) as-is.
            # batch_read_content should return None for missing items, but if a backend
            # raises unexpectedly, let the caller see the real exception.
            raise
