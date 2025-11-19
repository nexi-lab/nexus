"""Metadata store interface for Nexus."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass
class FileMetadata:
    """File metadata information.

    Note: UNIX-style permissions (owner/group/mode) have been
    removed. All permissions are now managed through ReBAC relationships.

    v0.7.0 P0 SECURITY: Added tenant_id for defense-in-depth isolation.
    """

    path: str
    backend_name: str
    physical_path: str
    size: int
    etag: str | None = None
    mime_type: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    version: int = 1
    tenant_id: str | None = None  # P0 SECURITY: Defense-in-depth tenant isolation
    created_by: str | None = None  # User or agent ID who created/modified this version
    is_directory: bool = False  # Whether this path represents a directory

    def validate(self) -> None:
        """Validate file metadata before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate path
        if not self.path:
            raise ValidationError("path is required")

        if not self.path.startswith("/"):
            raise ValidationError(f"path must start with '/', got {self.path!r}", path=self.path)

        # Check for null bytes
        if "\x00" in self.path:
            raise ValidationError("path contains null bytes", path=self.path)

        # Validate backend_name
        if not self.backend_name:
            raise ValidationError("backend_name is required", path=self.path)

        # Validate physical_path
        if not self.physical_path:
            raise ValidationError("physical_path is required", path=self.path)

        # Validate size
        if self.size < 0:
            raise ValidationError(f"size cannot be negative, got {self.size}", path=self.path)

        # Validate version
        if self.version < 1:
            raise ValidationError(f"version must be >= 1, got {self.version}", path=self.path)


class MetadataStore(ABC):
    """
    Abstract interface for metadata storage.

    Stores mapping between virtual paths and backend physical locations.
    """

    @abstractmethod
    def get(self, path: str) -> FileMetadata | None:
        """
        Get metadata for a file.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        pass

    @abstractmethod
    def put(self, metadata: FileMetadata) -> None:
        """
        Store or update file metadata.

        Args:
            metadata: File metadata to store
        """
        pass

    @abstractmethod
    def delete(self, path: str) -> None:
        """
        Delete file metadata.

        Args:
            path: Virtual path
        """
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """
        Check if metadata exists for a path.

        Args:
            path: Virtual path

        Returns:
            True if metadata exists, False otherwise
        """
        pass

    @abstractmethod
    def list(self, prefix: str = "", recursive: bool = True) -> list[FileMetadata]:
        """
        List all files with given path prefix.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files. If False, only direct children.
                      PERFORMANCE: Non-recursive uses database-level filtering (v0.7.0)

        Returns:
            List of file metadata
        """
        pass

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """
        Get metadata for multiple files in a single query.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to FileMetadata (or None if not found)
        """
        # Default implementation: call get() for each path
        return {path: self.get(path) for path in paths}

    def delete_batch(self, paths: Sequence[str]) -> None:
        """
        Delete multiple files in a single transaction.

        Args:
            paths: List of virtual paths to delete
        """
        # Default implementation: call delete() for each path
        for path in paths:
            self.delete(path)

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        """
        Store or update multiple file metadata entries in a single transaction.

        Args:
            metadata_list: List of file metadata to store
        """
        # Default implementation: call put() for each metadata
        for metadata in metadata_list:
            self.put(metadata)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """
        Get content IDs (hashes) for multiple paths in a single query.

        This is useful for CAS (Content-Addressable Storage) deduplication
        where you need to check which files have the same content.

        Performance: Avoids N+1 queries by fetching all content hashes
        in one database query.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to content_hash (or None if file not found)

        Example:
            >>> hashes = store.batch_get_content_ids(["/a.txt", "/b.txt"])
            >>> if hashes["/a.txt"] == hashes["/b.txt"]:
            ...     print("Files have identical content!")
        """
        # Default implementation: call get() for each path and extract etag
        result = {}
        for path in paths:
            metadata = self.get(path)
            result[path] = metadata.etag if metadata else None
        return result

    @abstractmethod
    def close(self) -> None:
        """Close the metadata store and release resources."""
        pass
