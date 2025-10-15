"""Unified local filesystem backend with CAS and directory support."""

import errno
import hashlib
import json
import platform
import shutil
import tempfile
from pathlib import Path
from typing import Any

from nexus.backends.backend import Backend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError


class LocalBackend(Backend):
    """
    Unified local filesystem backend.

    Combines:
    - Content-addressable storage (CAS) for automatic deduplication
    - Directory operations for filesystem compatibility

    Storage structure:
        root/
        ├── cas/              # Content storage (by hash)
        │   ├── ab/
        │   │   └── cd/
        │   │       ├── abcd1234...ef56        # Content file
        │   │       └── abcd1234...ef56.meta   # Metadata (ref count)
        └── dirs/             # Virtual directory structure
            ├── workspace/
            └── projects/

    Features:
    - Content deduplication (same content stored once)
    - Reference counting for safe deletion
    - Atomic write operations
    - Thread-safe file locking
    - Directory support for compatibility
    """

    def __init__(self, root_path: str | Path):
        """
        Initialize local backend.

        Args:
            root_path: Root directory for storage
        """
        self.root_path = Path(root_path).resolve()
        self.cas_root = self.root_path / "cas"  # CAS content storage
        self.dir_root = self.root_path / "dirs"  # Directory structure
        self._ensure_roots()

    def _ensure_roots(self) -> None:
        """Create root directories if they don't exist."""
        try:
            self.cas_root.mkdir(parents=True, exist_ok=True)
            self.dir_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackendError(
                f"Failed to create root directories: {e}", backend="local", path=str(self.root_path)
            ) from e

    # === Content Operations (CAS) ===

    def _compute_hash(self, content: bytes) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content).hexdigest()

    def _hash_to_path(self, content_hash: str) -> Path:
        """
        Convert content hash to filesystem path.

        Uses two-level directory structure:
        cas/ab/cd/abcd1234...ef56

        Args:
            content_hash: SHA-256 hash as hex string

        Returns:
            Path object for content file
        """
        if len(content_hash) < 4:
            raise ValueError(f"Invalid hash length: {content_hash}")

        dir1 = content_hash[:2]
        dir2 = content_hash[2:4]

        return self.cas_root / dir1 / dir2 / content_hash

    def _get_meta_path(self, content_hash: str) -> Path:
        """Get path to metadata file for content."""
        content_path = self._hash_to_path(content_hash)
        return content_path.with_suffix(".meta")

    def _read_metadata(self, content_hash: str) -> dict[str, Any]:
        """Read metadata for content."""
        meta_path = self._get_meta_path(content_hash)

        if not meta_path.exists():
            return {"ref_count": 0, "size": 0}

        try:
            # Read directly without locking (metadata files are small and atomic)
            content = meta_path.read_text(encoding="utf-8")
            result: dict[str, Any] = json.loads(content)
            return result
        except (OSError, json.JSONDecodeError) as e:
            raise BackendError(
                f"Failed to read metadata: {e}", backend="local", path=content_hash
            ) from e

    def _write_metadata(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Write metadata for content."""
        meta_path = self._get_meta_path(content_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Atomic write: write to temp file, then move
            # This avoids Windows file locking issues
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=meta_path.parent, delete=False, suffix=".tmp"
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(json.dumps(metadata))
                tmp_file.flush()

            # Atomic move (replace)
            shutil.move(str(tmp_path), str(meta_path))
        except OSError as e:
            if "tmp_path" in locals():
                Path(tmp_path).unlink(missing_ok=True)
            raise BackendError(
                f"Failed to write metadata: {e}", backend="local", path=content_hash
            ) from e

    def _lock_file(self, path: Path) -> "FileLock":
        """Acquire lock on file."""
        return FileLock(path)

    def write_content(self, content: bytes) -> str:
        """
        Write content to CAS storage and return its hash.

        If content already exists, increments reference count.
        """
        content_hash = self._compute_hash(content)
        content_path = self._hash_to_path(content_hash)

        # Check if content already exists
        if content_path.exists():
            metadata = self._read_metadata(content_hash)
            metadata["ref_count"] = metadata.get("ref_count", 0) + 1
            self._write_metadata(content_hash, metadata)
            return content_hash

        # Content doesn't exist - write atomically
        try:
            content_path.parent.mkdir(parents=True, exist_ok=True)

            # Write to temp file first
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=content_path.parent, delete=False
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(content)

            # Move to final location
            shutil.move(str(tmp_path), str(content_path))

            # Create metadata
            metadata = {"ref_count": 1, "size": len(content)}
            self._write_metadata(content_hash, metadata)

            return content_hash

        except OSError as e:
            if "tmp_path" in locals():
                tmp_path.unlink(missing_ok=True)

            raise BackendError(
                f"Failed to write content: {e}", backend="local", path=content_hash
            ) from e

    def read_content(self, content_hash: str) -> bytes:
        """Read content by its hash."""
        content_path = self._hash_to_path(content_hash)

        if not content_path.exists():
            raise NexusFileNotFoundError(content_hash)

        try:
            # Read directly without locking (content files are immutable after creation)
            content = content_path.read_bytes()

            # Verify hash
            actual_hash = self._compute_hash(content)
            if actual_hash != content_hash:
                raise BackendError(
                    f"Content hash mismatch: expected {content_hash}, got {actual_hash}",
                    backend="local",
                    path=content_hash,
                )

            return content

        except OSError as e:
            raise BackendError(
                f"Failed to read content: {e}", backend="local", path=content_hash
            ) from e

    def delete_content(self, content_hash: str) -> None:
        """Delete content by hash with reference counting."""
        content_path = self._hash_to_path(content_hash)

        if not content_path.exists():
            raise NexusFileNotFoundError(content_hash)

        try:
            metadata = self._read_metadata(content_hash)
            ref_count = metadata.get("ref_count", 1)

            if ref_count <= 1:
                # Last reference - delete file and metadata
                content_path.unlink()

                meta_path = self._get_meta_path(content_hash)
                if meta_path.exists():
                    meta_path.unlink()

                # Clean up empty directories
                self._cleanup_empty_dirs(content_path.parent)
            else:
                # Decrement reference count
                metadata["ref_count"] = ref_count - 1
                self._write_metadata(content_hash, metadata)

        except OSError as e:
            raise BackendError(
                f"Failed to delete content: {e}", backend="local", path=content_hash
            ) from e

    def _cleanup_empty_dirs(self, dir_path: Path) -> None:
        """Remove empty parent directories up to CAS root."""
        try:
            current = dir_path
            while current != self.cas_root and current.exists():
                if not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
        except OSError:
            pass

    def content_exists(self, content_hash: str) -> bool:
        """Check if content exists."""
        content_path = self._hash_to_path(content_hash)
        return content_path.exists()

    def get_content_size(self, content_hash: str) -> int:
        """Get content size in bytes."""
        content_path = self._hash_to_path(content_hash)

        if not content_path.exists():
            raise NexusFileNotFoundError(content_hash)

        try:
            return content_path.stat().st_size
        except OSError as e:
            raise BackendError(
                f"Failed to get content size: {e}", backend="local", path=content_hash
            ) from e

    def get_ref_count(self, content_hash: str) -> int:
        """Get reference count for content."""
        if not self.content_exists(content_hash):
            raise NexusFileNotFoundError(content_hash)

        metadata = self._read_metadata(content_hash)
        return int(metadata.get("ref_count", 0))

    # === Directory Operations ===

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create directory in virtual directory structure."""
        full_path = self.dir_root / path.lstrip("/")

        try:
            if parents:
                full_path.mkdir(parents=True, exist_ok=exist_ok)
            else:
                full_path.mkdir(exist_ok=exist_ok)
        except FileExistsError as e:
            if not exist_ok:
                raise e
        except FileNotFoundError as e:
            raise BackendError(
                f"Parent directory not found: {path}", backend="local", path=path
            ) from e
        except OSError as e:
            raise BackendError(
                f"Failed to create directory: {e}", backend="local", path=path
            ) from e

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove directory from virtual directory structure."""
        full_path = self.dir_root / path.lstrip("/")

        if not full_path.exists():
            raise NexusFileNotFoundError(path)

        if not full_path.is_dir():
            raise BackendError(f"Path is not a directory: {path}", backend="local", path=path)

        try:
            if recursive:
                shutil.rmtree(full_path)
            else:
                full_path.rmdir()
        except OSError as e:
            # Re-raise OSError for "directory not empty"
            if e.errno in (errno.ENOTEMPTY, 66):  # errno.ENOTEMPTY or macOS errno 66
                raise
            raise BackendError(
                f"Failed to remove directory: {e}", backend="local", path=path
            ) from e

    def is_directory(self, path: str) -> bool:
        """Check if path is a directory."""
        try:
            full_path = self.dir_root / path.lstrip("/")
            return full_path.exists() and full_path.is_dir()
        except Exception:
            return False


class FileLock:
    """
    Context manager for file locking.

    Uses fcntl.flock on POSIX systems and msvcrt.locking on Windows.
    """

    def __init__(self, path: Path, timeout: float = 10.0):
        """Initialize file lock."""
        self.path = path
        self.timeout = timeout
        self.lock_file: Any = None

    def __enter__(self) -> "FileLock":
        """Acquire lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Use a+b mode: append+binary with read/write
        # This works better on Windows for newly created files
        self.lock_file = open(self.path, "a+b")

        # Platform-specific locking
        if platform.system() == "Windows":
            import msvcrt

            msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Release lock."""
        if self.lock_file:
            # Platform-specific unlocking
            if platform.system() == "Windows":
                import msvcrt

                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)

            self.lock_file.close()
