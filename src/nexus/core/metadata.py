"""Metadata store interface for Nexus."""

from abc import ABC, abstractmethod
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

    @abstractmethod
    def close(self) -> None:
        """Close the metadata store and release resources."""
        pass
