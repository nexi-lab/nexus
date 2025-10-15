"""Embedded mode implementation for Nexus."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.core.backend import StorageBackend
from nexus.core.backends.local import LocalBackend
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError
from nexus.core.metadata import FileMetadata
from nexus.core.router import PathRouter
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class Embedded:
    """
    Embedded mode filesystem for Nexus.

    Provides file operations (read, write, delete) with metadata tracking
    and support for multiple storage backends.
    """

    def __init__(
        self,
        data_dir: str | Path = "./nexus-data",
        db_path: str | Path | None = None,
    ):
        """
        Initialize embedded filesystem.

        Args:
            data_dir: Root directory for storing files
            db_path: Path to SQLite metadata database (auto-generated if None)
        """
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Initialize metadata store (using new SQLAlchemy-based store)
        if db_path is None:
            db_path = self.data_dir / "metadata.db"
        self.metadata = SQLAlchemyMetadataStore(db_path)

        # Initialize path router with default mounts
        self.router = PathRouter()

        # Default mount: all paths to local backend
        self.backend: StorageBackend = LocalBackend(self.data_dir / "files")
        self.router.add_mount("/", self.backend, priority=0)

    def _validate_path(self, path: str) -> str:
        """
        Validate virtual path.

        Args:
            path: Virtual path to validate

        Returns:
            Normalized path

        Raises:
            InvalidPathError: If path is invalid
        """
        if not path:
            raise InvalidPathError("", "Path cannot be empty")

        # Ensure path starts with /
        if not path.startswith("/"):
            path = "/" + path

        # Check for invalid characters
        invalid_chars = ["\0", "\n", "\r"]
        for char in invalid_chars:
            if char in path:
                raise InvalidPathError(path, f"Path contains invalid character: {repr(char)}")

        # Check for parent directory traversal
        if ".." in path:
            raise InvalidPathError(path, "Path contains '..' segments")

        return path

    def _compute_etag(self, content: bytes) -> str:
        """
        Compute ETag for file content.

        Args:
            content: File content

        Returns:
            ETag (MD5 hash)
        """
        return hashlib.md5(content).hexdigest()

    def read(self, path: str) -> bytes:
        """
        Read file content as bytes.

        Args:
            path: Virtual path to read

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
        """
        path = self._validate_path(path)

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Read from backend
        content = self.backend.read(meta.physical_path)

        return content

    def write(self, path: str, content: bytes) -> None:
        """
        Write content to a file.

        Creates parent directories if needed. Overwrites existing files.
        Updates metadata store.

        Args:
            path: Virtual path to write
            content: File content as bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
        """
        path = self._validate_path(path)

        # Generate physical path (strip leading slash)
        physical_path = path.lstrip("/")

        # Write to backend
        self.backend.write(physical_path, content)

        # Update metadata
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        if meta is None:
            # New file
            metadata = FileMetadata(
                path=path,
                backend_name="local",
                physical_path=physical_path,
                size=len(content),
                etag=self._compute_etag(content),
                created_at=now,
                modified_at=now,
                version=1,
            )
        else:
            # Update existing file
            # Note: Version tracking not implemented in v0.1.0 simplified schema
            metadata = FileMetadata(
                path=path,
                backend_name=meta.backend_name,
                physical_path=physical_path,
                size=len(content),
                etag=self._compute_etag(content),
                created_at=meta.created_at,
                modified_at=now,
                version=1,  # Version tracking will be added in v0.2.0
            )

        self.metadata.put(metadata)

    def delete(self, path: str) -> None:
        """
        Delete a file.

        Removes file from backend and metadata store.

        Args:
            path: Virtual path to delete

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If delete operation fails
        """
        path = self._validate_path(path)

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Delete from backend
        self.backend.delete(meta.physical_path)

        # Remove from metadata
        self.metadata.delete(path)

    def exists(self, path: str) -> bool:
        """
        Check if a file exists.

        Args:
            path: Virtual path to check

        Returns:
            True if file exists, False otherwise
        """
        try:
            path = self._validate_path(path)
            return self.metadata.exists(path)
        except InvalidPathError:
            return False

    def list(self, prefix: str = "") -> list[str]:
        """
        List all files with given path prefix.

        Args:
            prefix: Path prefix to filter by

        Returns:
            List of virtual paths
        """
        if prefix:
            prefix = self._validate_path(prefix)

        metadata_list = self.metadata.list(prefix)
        return [meta.path for meta in metadata_list]

    # === Directory Operations ===

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """
        Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            InvalidPathError: If path is invalid
            BackendError: If operation fails
        """
        path = self._validate_path(path)

        # Route to backend
        route = self.router.route(path)

        # Create directory in backend
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=exist_ok)

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove a directory.

        Args:
            path: Virtual path to directory
            recursive: Remove non-empty directory (like rm -rf)

        Raises:
            OSError: If directory not empty and recursive=False
            NexusFileNotFoundError: If directory doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If operation fails
        """
        path = self._validate_path(path)

        # Route to backend
        route = self.router.route(path)

        # Check readonly
        if route.readonly:
            raise PermissionError(f"Mount is readonly: {route.mount_point}")

        # Remove directory in backend
        route.backend.rmdir(route.backend_path, recursive=recursive)

    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Virtual path to check

        Returns:
            True if path is a directory, False otherwise
        """
        try:
            path = self._validate_path(path)
            route = self.router.route(path)
            return route.backend.is_directory(route.backend_path)
        except (InvalidPathError, Exception):
            return False

    def close(self) -> None:
        """Close the embedded filesystem and release resources."""
        self.metadata.close()

    def __enter__(self) -> "Embedded":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
