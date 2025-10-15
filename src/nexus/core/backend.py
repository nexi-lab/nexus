"""Storage backend interface for Nexus."""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """
    Abstract interface for storage backends.

    All storage backends (LocalFS, S3, GCS, etc.) must implement this interface.
    """

    @abstractmethod
    def read(self, path: str) -> bytes:
        """
        Read entire file content as bytes.

        Args:
            path: Physical path within the backend

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read operation fails
        """
        pass

    @abstractmethod
    def write(self, path: str, content: bytes) -> None:
        """
        Write content to a file.

        Creates parent directories if needed. Overwrites existing files.

        Args:
            path: Physical path within the backend
            content: File content as bytes

        Raises:
            NexusPermissionError: If write is not allowed
            BackendError: If write operation fails
        """
        pass

    @abstractmethod
    def delete(self, path: str) -> None:
        """
        Delete a file.

        Args:
            path: Physical path within the backend

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If delete operation fails
        """
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """
        Check if a file exists.

        Args:
            path: Physical path within the backend

        Returns:
            True if file exists, False otherwise
        """
        pass

    @abstractmethod
    def get_size(self, path: str) -> int:
        """
        Get file size in bytes.

        Args:
            path: Physical path within the backend

        Returns:
            File size in bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If operation fails
        """
        pass

    @abstractmethod
    def list_directory(self, path: str) -> list[str]:
        """
        List files in a directory.

        Args:
            path: Physical directory path within the backend

        Returns:
            List of file paths (relative to the directory)

        Raises:
            NexusFileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        pass

    # === Directory Operations ===

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """
        Create a directory.

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
