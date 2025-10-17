"""Metadata store interface for Nexus."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass
class FileMetadata:
    """File metadata information."""

    path: str
    backend_name: str
    physical_path: str
    size: int
    etag: str | None = None
    mime_type: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    version: int = 1


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
    def list(self, prefix: str = "") -> list[FileMetadata]:
        """
        List all files with given path prefix.

        Args:
            prefix: Path prefix to filter by

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

    @abstractmethod
    def close(self) -> None:
        """Close the metadata store and release resources."""
        pass
