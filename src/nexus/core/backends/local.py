"""Local filesystem backend implementation."""

from pathlib import Path

from nexus.core.backend import StorageBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError, NexusPermissionError


class LocalBackend(StorageBackend):
    """
    Local filesystem storage backend.

    Stores files on the local filesystem with a configurable root directory.
    """

    def __init__(self, root_path: str | Path):
        """
        Initialize local backend.

        Args:
            root_path: Root directory for storing files
        """
        self.root_path = Path(root_path).resolve()
        self._ensure_root_exists()

    def _ensure_root_exists(self) -> None:
        """Create root directory if it doesn't exist."""
        try:
            self.root_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackendError(
                f"Failed to create root directory: {e}", backend="local", path=str(self.root_path)
            ) from e

    def _resolve_path(self, path: str) -> Path:
        """
        Resolve virtual path to physical filesystem path.

        Args:
            path: Virtual path (relative to root)

        Returns:
            Absolute filesystem path

        Raises:
            BackendError: If path escapes root directory
        """
        # Remove leading slash if present
        path = path.lstrip("/")

        # Resolve to absolute path
        full_path = (self.root_path / path).resolve()

        # Ensure path is within root (prevent directory traversal)
        try:
            full_path.relative_to(self.root_path)
        except ValueError as e:
            raise BackendError(
                f"Path escapes root directory: {path}", backend="local", path=path
            ) from e

        return full_path

    def read(self, path: str) -> bytes:
        """Read file content as bytes."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            raise NexusFileNotFoundError(path)

        if not full_path.is_file():
            raise BackendError(f"Path is not a file: {path}", backend="local", path=path)

        try:
            return full_path.read_bytes()
        except PermissionError as e:
            raise NexusPermissionError(path, "Read permission denied") from e
        except OSError as e:
            raise BackendError(f"Failed to read file: {e}", backend="local", path=path) from e

    def write(self, path: str, content: bytes) -> None:
        """Write content to file."""
        full_path = self._resolve_path(path)

        # Create parent directories if needed
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackendError(
                f"Failed to create parent directories: {e}", backend="local", path=path
            ) from e

        # Write file content
        try:
            full_path.write_bytes(content)
        except PermissionError as e:
            raise NexusPermissionError(path, "Write permission denied") from e
        except OSError as e:
            raise BackendError(f"Failed to write file: {e}", backend="local", path=path) from e

    def delete(self, path: str) -> None:
        """Delete a file."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            raise NexusFileNotFoundError(path)

        if not full_path.is_file():
            raise BackendError(f"Path is not a file: {path}", backend="local", path=path)

        try:
            full_path.unlink()
        except PermissionError as e:
            raise NexusPermissionError(path, "Delete permission denied") from e
        except OSError as e:
            raise BackendError(f"Failed to delete file: {e}", backend="local", path=path) from e

    def exists(self, path: str) -> bool:
        """Check if file exists."""
        try:
            full_path = self._resolve_path(path)
            return full_path.exists() and full_path.is_file()
        except BackendError:
            return False

    def get_size(self, path: str) -> int:
        """Get file size in bytes."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            raise NexusFileNotFoundError(path)

        if not full_path.is_file():
            raise BackendError(f"Path is not a file: {path}", backend="local", path=path)

        try:
            return full_path.stat().st_size
        except OSError as e:
            raise BackendError(f"Failed to get file size: {e}", backend="local", path=path) from e

    def list_directory(self, path: str) -> list[str]:
        """List files in directory."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            raise NexusFileNotFoundError(path)

        if not full_path.is_dir():
            raise BackendError(f"Path is not a directory: {path}", backend="local", path=path)

        try:
            # Return relative paths
            return [str(p.relative_to(full_path)) for p in full_path.rglob("*") if p.is_file()]
        except PermissionError as e:
            raise NexusPermissionError(path, "List permission denied") from e
        except OSError as e:
            raise BackendError(f"Failed to list directory: {e}", backend="local", path=path) from e
