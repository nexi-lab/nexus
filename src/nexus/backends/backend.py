"""Unified backend interface for Nexus storage.

This module provides a single, unified interface for all storage backends,
combining content-addressable storage (CAS) with directory operations.
"""

from abc import ABC, abstractmethod


class Backend(ABC):
    """
    Unified backend interface for storage operations.

    All storage backends (LocalFS, S3, GCS, etc.) implement this interface.
    It combines:
    - Content-addressable storage (CAS) for automatic deduplication
    - Directory operations for filesystem compatibility

    Content Operations:
    - Files stored by SHA-256 hash
    - Automatic deduplication (same content = stored once)
    - Reference counting for safe deletion

    Directory Operations:
    - Virtual directory structure (metadata-based or backend-native)
    - Compatible with path router and mounting
    """

    # === Content Operations (CAS) ===

    @abstractmethod
    def write_content(self, content: bytes) -> str:
        """
        Write content to storage and return its content hash.

        If content already exists (same hash), increments reference count
        instead of writing duplicate data.

        Args:
            content: File content as bytes

        Returns:
            Content hash (SHA-256 as hex string)

        Raises:
            BackendError: If write operation fails
        """
        pass

    @abstractmethod
    def read_content(self, content_hash: str) -> bytes:
        """
        Read content by its hash.

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If read operation fails
        """
        pass

    @abstractmethod
    def delete_content(self, content_hash: str) -> None:
        """
        Delete content by hash.

        Decrements reference count. Only deletes actual file when
        reference count reaches zero.

        Args:
            content_hash: SHA-256 hash as hex string

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If delete operation fails
        """
        pass

    @abstractmethod
    def content_exists(self, content_hash: str) -> bool:
        """
        Check if content exists.

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            True if content exists, False otherwise
        """
        pass

    @abstractmethod
    def get_content_size(self, content_hash: str) -> int:
        """
        Get content size in bytes.

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            Content size in bytes

        Raises:
            NexusFileNotFoundError: If content doesn't exist
            BackendError: If operation fails
        """
        pass

    @abstractmethod
    def get_ref_count(self, content_hash: str) -> int:
        """
        Get reference count for content.

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            Number of references to this content

        Raises:
            NexusFileNotFoundError: If content doesn't exist
        """
        pass

    # === Directory Operations ===

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """
        Create a directory.

        For backends without native directory support (e.g., S3),
        this may be a no-op or create marker objects.

        Args:
            path: Directory path (relative to backend root)
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            BackendError: If operation fails
        """
        pass

    @abstractmethod
    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove a directory.

        Args:
            path: Directory path
            recursive: Remove non-empty directory (like rm -rf)

        Raises:
            OSError: If directory not empty and recursive=False
            NexusFileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        pass

    @abstractmethod
    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Path to check

        Returns:
            True if path is a directory, False otherwise
        """
        pass
