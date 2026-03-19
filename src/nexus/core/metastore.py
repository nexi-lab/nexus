"""MetastoreABC — the Metastore storage pillar.

One of the Four Storage Pillars (NEXUS-LEGO-ARCHITECTURE.md §2.0):
  - MetastoreABC  (required at boot — this file)
  - ObjectStoreABC (mounted post-init — backends/backend.py)
  - RecordStoreABC (services-only — storage/record_store.py)
  - CacheStoreABC  (optional — core/cache_store.py)

MetastoreABC is the kernel's inode layer: the typed contract between
VFS and the underlying ordered KV store.  The kernel cannot describe
files without it.  Linux analogue: ``struct inode_operations``.

Implementations:
  - RaftMetadataStore  (storage/raft_metadata_store.py)
  - FederatedMetadataProxy (raft/federated_metadata_proxy.py)

SSOT: proto/nexus/core/metadata.proto defines the FileMetadata fields.
This ABC defines the *operations* over those fields.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata


class MetastoreABC(ABC):
    """Abstract base class for metadata storage (the "Metastore" pillar).

    Stores mapping between virtual paths and backend physical locations.
    All metastore backends (Raft, Federated, etc.) implement this interface.

    Abstract methods (must override):
        get, put, delete, exists, list, close

    Concrete methods (may override for performance):
        is_committed, list_iter,
        get_batch, delete_batch, put_batch, batch_get_content_ids
    """

    @abstractmethod
    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file."""
        pass

    @abstractmethod
    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store or update file metadata.

        Returns:
            EC mode: write token (int) for polling via is_committed().
            SC mode: None (write is already committed when this returns).
        """
        pass

    def is_committed(self, _token: int) -> str | None:
        """Check if an EC write token has been replicated to a majority.

        Args:
            token: Write token returned by put() with consistency="ec".

        Returns:
            "committed" — replicated to majority.
            "pending" — local only, awaiting replication.
            None — invalid token or no replication log.
        """
        return None

    @abstractmethod
    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete file metadata. Returns deleted file info or None."""
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if metadata exists for a path."""
        pass

    @abstractmethod
    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        """List all files with given path prefix."""
        pass

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        """Iterate over file metadata matching prefix.

        Memory-efficient alternative to list(). Yields results one at a time
        instead of materializing the full list in memory.

        Subclasses may override for true streaming from the underlying store.
        The default implementation delegates to list() for backward compatibility.
        """
        yield from self.list(prefix, recursive, **kwargs)

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Get metadata for multiple files in a single query."""
        return {path: self.get(path) for path in paths}

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Delete multiple files in a single transaction."""
        for path in paths:
            self.delete(path)

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        """Store or update multiple file metadata entries in a single transaction."""
        for metadata in metadata_list:
            self.put(metadata)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """Get content IDs (hashes) for multiple paths in a single query."""
        result: dict[str, str | None] = {}
        for path in paths:
            metadata = self.get(path)
            result[path] = metadata.etag if metadata else None
        return result

    @abstractmethod
    def close(self) -> None:
        """Close the metadata store and release resources."""
        pass
