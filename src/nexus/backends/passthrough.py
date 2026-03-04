"""Passthrough backend with stable pointer paths for file watching.

This backend uses a two-layer storage structure:
1. Pointers layer: Stable file paths that can be watched via inotify/ReadDirectoryChangesW
2. CAS layer: Content-addressed storage for deduplication

Storage structure:
    base_path/
    ├── pointers/           # Stable paths (inotify-watchable)
    │   └── inbox/
    │       └── file.txt    # Content: "cas:abcd1234..."
    └── cas/                # Content-addressed storage (dedup)
        └── ab/cd/abcd1234...

This enables efficient file watching for same-box scenarios where clients
can use OS-native APIs (inotify on Linux, ReadDirectoryChangesW on Windows)
to detect changes without polling.
"""

import contextlib
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.backends.backend import Backend
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.contracts.capabilities import ConnectorCapability
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Pointer file format prefix
POINTER_PREFIX = "cas:"


@register_connector(
    "passthrough",
    description="Passthrough backend with stable paths for file watching",
    category="storage",
)
class PassthroughBackend(Backend):
    """Passthrough backend with stable pointer paths for same-box file watching.

    This backend separates stable file paths (pointers) from content storage (CAS),
    enabling efficient OS-native file watching while maintaining content deduplication.

    Key features:
    - Stable pointer paths for inotify/ReadDirectoryChangesW watching
    - Content-addressed storage (CAS) for automatic deduplication
    - Atomic pointer updates (temp file + rename) for single watch events
    - In-memory advisory locking for same-box coordination

    Storage structure:
        base_path/
        ├── pointers/           # Stable paths that can be watched
        │   └── inbox/
        │       └── file.txt    # Contains: "cas:abcd1234...\\n"
        └── cas/                # Content-addressed storage
            └── ab/cd/abcd1234...

    Example:
        >>> backend = PassthroughBackend("/data/nexus")
        >>> # Write content - stores in CAS, creates pointer
        >>> content_hash = backend.write_content(b"hello").content_hash
    """

    _CAPABILITIES = frozenset(
        {
            ConnectorCapability.PASSTHROUGH,
            ConnectorCapability.ROOT_PATH,
        }
    )

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "base_path": ConnectionArg(
            type=ArgType.PATH,
            description="Base directory for storage (pointers/ and cas/ subdirs)",
            required=True,
            config_key="data_dir",
        ),
    }

    def __init__(self, base_path: str | Path) -> None:
        """Initialize passthrough backend.

        Args:
            base_path: Base directory for storage. Will create pointers/ and cas/ subdirs.
        """
        self.base_path = Path(base_path).resolve()
        self.pointers_root = self.base_path / "pointers"
        self.cas_root = self.base_path / "cas"

        self._ensure_roots()

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "passthrough"

    # --- Capability flags ---

    @property
    def is_passthrough(self) -> bool:
        """PassthroughBackend supports same-box file watching."""
        return True

    def _ensure_roots(self) -> None:
        """Create root directories if they don't exist."""
        try:
            self.pointers_root.mkdir(parents=True, exist_ok=True)
            self.cas_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackendError(
                f"Failed to create root directories: {e}",
                backend="passthrough",
                path=str(self.base_path),
            ) from e

    # === Internal Pointer Operations ===

    def _get_pointer_path(self, virtual_path: str) -> Path:
        """Convert virtual path to physical pointer file path."""
        clean_path = virtual_path.lstrip("/")
        if not clean_path:
            return self.pointers_root
        return self.pointers_root / clean_path

    def _get_cas_path(self, content_hash: str) -> Path:
        """Convert content hash to CAS storage path (two-level directory)."""
        if len(content_hash) < 4:
            raise ValueError(f"Invalid hash length: {content_hash}")
        dir1 = content_hash[:2]
        dir2 = content_hash[2:4]
        return self.cas_root / dir1 / dir2 / content_hash

    def _write_pointer(self, virtual_path: str, content_hash: str) -> None:
        """Atomically write/update a pointer file (temp + os.replace)."""
        pointer_path = self._get_pointer_path(virtual_path)
        pointer_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=pointer_path.parent,
                delete=False,
                suffix=".tmp",
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(f"{POINTER_PREFIX}{content_hash}\n")
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            os.replace(str(tmp_path), str(pointer_path))
            tmp_path = None
            logger.debug("Wrote pointer: %s -> %s", virtual_path, content_hash)

        except OSError as e:
            raise BackendError(
                f"Failed to write pointer: {e}",
                backend="passthrough",
                path=virtual_path,
            ) from e
        finally:
            if tmp_path is not None and tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def _read_pointer(self, virtual_path: str) -> str | None:
        """Read CAS hash from a pointer file."""
        pointer_path = self._get_pointer_path(virtual_path)

        if not pointer_path.exists() or pointer_path.is_dir():
            return None

        try:
            content = pointer_path.read_text(encoding="utf-8").strip()
            if content.startswith(POINTER_PREFIX):
                return content[len(POINTER_PREFIX) :]
            logger.warning("Invalid pointer format at %s: %s", virtual_path, content[:50])
            return None
        except OSError as e:
            logger.warning("Failed to read pointer %s: %s", virtual_path, e)
            return None

    def _delete_pointer(self, virtual_path: str) -> bool:
        """Delete a pointer file."""
        pointer_path = self._get_pointer_path(virtual_path)

        if not pointer_path.exists():
            return False

        try:
            pointer_path.unlink()
            self._cleanup_empty_dirs(pointer_path.parent, self.pointers_root)
            return True
        except OSError as e:
            raise BackendError(
                f"Failed to delete pointer: {e}",
                backend="passthrough",
                path=virtual_path,
            ) from e

    def _cleanup_empty_dirs(self, dir_path: Path, stop_at: Path) -> None:
        """Remove empty parent directories up to stop_at."""
        try:
            current = dir_path
            while current != stop_at and current.exists():
                if not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
        except OSError:
            pass

    # === Public API for File Watching ===

    def get_physical_path(self, virtual_path: str) -> Path:
        """Get the physical pointer file path for a virtual path.

        Used by NexusFS to set up file watches on the correct path.

        Args:
            virtual_path: Virtual path (e.g., "/inbox/file.txt")

        Returns:
            Physical path to the pointer file or directory
        """
        return self._get_pointer_path(virtual_path)

    # === Backend Interface Implementation ===

    def write_content(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Write content to CAS and create/update pointer if virtual_path in context.

        Returns:
            WriteResult with content_hash and size.

        Raises:
            BackendError: If write operation fails.
        """
        content_hash = hash_content(content)
        cas_path = self._get_cas_path(content_hash)

        # Write to CAS if not exists
        if not cas_path.exists():
            cas_path.parent.mkdir(parents=True, exist_ok=True)

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=cas_path.parent,
                    delete=False,
                ) as tmp_file:
                    tmp_path = Path(tmp_file.name)
                    tmp_file.write(content)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())

                os.replace(str(tmp_path), str(cas_path))
                tmp_path = None
                logger.debug("Wrote CAS content: %s", content_hash)

            except OSError as e:
                raise BackendError(
                    f"Failed to write CAS content: {e}",
                    backend="passthrough",
                    path=content_hash,
                ) from e
            finally:
                if tmp_path is not None and tmp_path.exists():
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()

        # Write pointer if virtual_path is provided in context
        virtual_path = getattr(context, "virtual_path", None) if context else None
        if virtual_path:
            self._write_pointer(virtual_path, content_hash)

        return WriteResult(content_hash=content_hash, size=len(content))

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read content from CAS by hash (or via pointer if hash is empty).

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If read operation fails.
        """
        # If no hash provided, try to read via pointer
        if not content_hash and context:
            virtual_path = getattr(context, "virtual_path", None)
            if virtual_path:
                content_hash_from_pointer = self._read_pointer(virtual_path)
                if content_hash_from_pointer:
                    content_hash = content_hash_from_pointer
                else:
                    raise NexusFileNotFoundError(
                        path=virtual_path,
                        message=f"File not found: {virtual_path}",
                    )

        if not content_hash:
            raise BackendError(
                "No content hash provided",
                backend="passthrough",
            )

        cas_path = self._get_cas_path(content_hash)

        if not cas_path.exists():
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        try:
            content = cas_path.read_bytes()
        except OSError as e:
            raise BackendError(
                f"Failed to read CAS content: {e}",
                backend="passthrough",
                path=content_hash,
            ) from e

        # Verify hash
        actual_hash = hash_content(content)
        if actual_hash != content_hash:
            raise BackendError(
                f"Content hash mismatch: expected {content_hash}, got {actual_hash}",
                backend="passthrough",
                path=content_hash,
            )

        return content

    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """Delete pointer (CAS cleanup deferred to GC)."""
        virtual_path = getattr(context, "virtual_path", None) if context else None
        if virtual_path:
            self._delete_pointer(virtual_path)

    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Check if content exists in CAS."""
        cas_path = self._get_cas_path(content_hash)
        return cas_path.exists()

    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """Get content size in bytes.

        Raises:
            NexusFileNotFoundError: If content does not exist.
            BackendError: If stat operation fails.
        """
        cas_path = self._get_cas_path(content_hash)

        if not cas_path.exists():
            raise NexusFileNotFoundError(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
            )

        try:
            return cas_path.stat().st_size
        except OSError as e:
            raise BackendError(
                f"Failed to get content size: {e}",
                backend="passthrough",
                path=content_hash,
            ) from e

    def get_ref_count(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """Get reference count (returns 1 if exists, 0 otherwise)."""
        cas_path = self._get_cas_path(content_hash)
        return 1 if cas_path.exists() else 0

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create a directory in the pointers layer.

        Raises:
            BackendError: If directory creation fails.
        """
        dir_path = self._get_pointer_path(path)

        try:
            dir_path.mkdir(parents=parents, exist_ok=exist_ok)
        except FileExistsError:
            if exist_ok:
                return
            raise BackendError(
                f"Directory already exists: {path}",
                backend="passthrough",
                path=path,
            ) from None
        except FileNotFoundError:
            raise BackendError(
                f"Parent directory not found: {path}",
                backend="passthrough",
                path=path,
            ) from None
        except OSError as e:
            raise BackendError(
                f"Failed to create directory: {e}",
                backend="passthrough",
                path=path,
            ) from e

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Remove a directory from the pointers layer.

        Raises:
            NexusFileNotFoundError: If directory does not exist.
            BackendError: If path is not a directory or removal fails.
        """
        import shutil

        dir_path = self._get_pointer_path(path)

        if not dir_path.exists():
            raise NexusFileNotFoundError(
                path=path,
                message=f"Directory not found: {path}",
            )

        if not dir_path.is_dir():
            raise BackendError(
                f"Path is not a directory: {path}",
                backend="passthrough",
                path=path,
            )

        try:
            if recursive:
                shutil.rmtree(dir_path)
            else:
                dir_path.rmdir()
        except OSError as e:
            raise BackendError(
                f"Failed to remove directory: {e}",
                backend="passthrough",
                path=path,
            ) from e

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Check if path is a directory."""
        dir_path = self._get_pointer_path(path)
        return dir_path.exists() and dir_path.is_dir()

    def list_dir(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """List directory contents."""
        dir_path = self._get_pointer_path(path)

        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not dir_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        entries = []
        for entry in dir_path.iterdir():
            name = entry.name
            if name.endswith(".tmp"):
                continue
            if entry.is_dir():
                name += "/"
            entries.append(name)

        return sorted(entries)

    # Advisory locking removed — use LockManagerBase (LocalLockManager
    # or RaftLockManager) via factory DI. See lock-architecture.md.
