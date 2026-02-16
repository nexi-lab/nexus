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

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.backends.backend import Backend
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError
from nexus.core.hash_fast import hash_content
from nexus.core.response import HandlerResponse, timed_response

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.permissions.permissions_enhanced import EnhancedOperationContext

logger = logging.getLogger(__name__)

# Pointer file format prefix
POINTER_PREFIX = "cas:"


@dataclass
class _LockInfo:
    """Internal lock information for a single holder."""

    lock_id: str
    path: str
    acquired_at: float


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
        >>> content_hash = backend.write_content(b"hello").unwrap()
    """

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

        # In-memory lock manager for same-box locking (supports multi-slot)
        # path -> list of holders (empty list = unlocked, mutex = max 1 holder)
        self._locks: dict[str, list[_LockInfo]] = {}
        # path -> max_holders (SSOT for consistency checking)
        self._lock_limits: dict[str, int] = {}
        self._locks_mutex = threading.Lock()

        self._ensure_roots()

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "passthrough"

    # --- Capability flags ---

    @property
    def is_passthrough(self) -> bool:
        """PassthroughBackend supports same-box file watching and locking."""
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
            logger.debug(f"Wrote pointer: {virtual_path} -> {content_hash}")

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
            logger.warning(f"Invalid pointer format at {virtual_path}: {content[:50]}")
            return None
        except OSError as e:
            logger.warning(f"Failed to read pointer {virtual_path}: {e}")
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

    @timed_response
    def write_content(
        self,
        content: bytes,
        context: OperationContext | None = None,
    ) -> HandlerResponse[str]:
        """Write content to CAS and create/update pointer if virtual_path in context."""
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
                logger.debug(f"Wrote CAS content: {content_hash}")

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

        return HandlerResponse.ok(
            data=content_hash,
            backend_name=self.name,
            path=virtual_path or content_hash,
        )

    @timed_response
    def read_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bytes]:
        """Read content from CAS by hash (or via pointer if hash is empty)."""
        # If no hash provided, try to read via pointer
        if not content_hash and context:
            virtual_path = getattr(context, "virtual_path", None)
            if virtual_path:
                content_hash_from_pointer = self._read_pointer(virtual_path)
                if content_hash_from_pointer:
                    content_hash = content_hash_from_pointer
                else:
                    return HandlerResponse.not_found(
                        path=virtual_path,
                        message=f"File not found: {virtual_path}",
                        backend_name=self.name,
                    )

        if not content_hash:
            return HandlerResponse.error(
                message="No content hash provided",
                code=400,
                backend_name=self.name,
                path="",
            )

        cas_path = self._get_cas_path(content_hash)

        if not cas_path.exists():
            return HandlerResponse.not_found(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
                backend_name=self.name,
            )

        content = cas_path.read_bytes()

        # Verify hash
        actual_hash = hash_content(content)
        if actual_hash != content_hash:
            return HandlerResponse.error(
                message=f"Content hash mismatch: expected {content_hash}, got {actual_hash}",
                code=500,
                backend_name=self.name,
                path=content_hash,
            )

        return HandlerResponse.ok(
            data=content,
            backend_name=self.name,
            path=content_hash,
        )

    @timed_response
    def delete_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Delete pointer (CAS cleanup deferred to GC)."""
        virtual_path = getattr(context, "virtual_path", None) if context else None
        if virtual_path:
            self._delete_pointer(virtual_path)

        return HandlerResponse.ok(
            data=None,
            backend_name=self.name,
            path=virtual_path or content_hash,
        )

    @timed_response
    def content_exists(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bool]:
        """Check if content exists in CAS."""
        cas_path = self._get_cas_path(content_hash)

        return HandlerResponse.ok(
            data=cas_path.exists(),
            backend_name=self.name,
            path=content_hash,
        )

    @timed_response
    def get_content_size(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[int]:
        """Get content size in bytes."""
        cas_path = self._get_cas_path(content_hash)

        if not cas_path.exists():
            return HandlerResponse.not_found(
                path=content_hash,
                message=f"CAS content not found: {content_hash}",
                backend_name=self.name,
            )

        size = cas_path.stat().st_size
        return HandlerResponse.ok(
            data=size,
            backend_name=self.name,
            path=content_hash,
        )

    @timed_response
    def get_ref_count(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[int]:
        """Get reference count (returns 1 if exists, 0 otherwise)."""
        cas_path = self._get_cas_path(content_hash)

        return HandlerResponse.ok(
            data=1 if cas_path.exists() else 0,
            backend_name=self.name,
            path=content_hash,
        )

    # === Directory Operations ===

    @timed_response
    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Create a directory in the pointers layer."""
        dir_path = self._get_pointer_path(path)

        try:
            dir_path.mkdir(parents=parents, exist_ok=exist_ok)
            return HandlerResponse.ok(
                data=None,
                backend_name=self.name,
                path=path,
            )
        except FileExistsError:
            if exist_ok:
                return HandlerResponse.ok(
                    data=None,
                    backend_name=self.name,
                    path=path,
                )
            return HandlerResponse.error(
                message=f"Directory already exists: {path}",
                code=409,
                is_expected=True,
                backend_name=self.name,
                path=path,
            )
        except FileNotFoundError:
            return HandlerResponse.error(
                message=f"Parent directory not found: {path}",
                code=404,
                is_expected=True,
                backend_name=self.name,
                path=path,
            )

    @timed_response
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Remove a directory from the pointers layer."""
        import shutil

        dir_path = self._get_pointer_path(path)

        if not dir_path.exists():
            return HandlerResponse.not_found(
                path=path,
                message=f"Directory not found: {path}",
                backend_name=self.name,
            )

        if not dir_path.is_dir():
            return HandlerResponse.error(
                message=f"Path is not a directory: {path}",
                code=400,
                is_expected=True,
                backend_name=self.name,
                path=path,
            )

        if recursive:
            shutil.rmtree(dir_path)
        else:
            dir_path.rmdir()

        return HandlerResponse.ok(
            data=None,
            backend_name=self.name,
            path=path,
        )

    @timed_response
    def is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bool]:
        """Check if path is a directory."""
        dir_path = self._get_pointer_path(path)

        return HandlerResponse.ok(
            data=dir_path.exists() and dir_path.is_dir(),
            backend_name=self.name,
            path=path,
        )

    def list_dir(
        self,
        path: str,
        context: OperationContext | None = None,
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

    # === Locking Operations (In-Memory for Same-Box) ===

    def lock(self, path: str, timeout: float = 30.0, max_holders: int = 1) -> str | None:
        """Acquire an advisory lock on a path.

        Supports both mutex (max_holders=1) and semaphore (max_holders>1) modes.

        Args:
            path: Virtual path to lock
            timeout: Maximum time to wait for lock (seconds)
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)

        Returns:
            Lock ID if acquired, None if timeout

        Raises:
            ValueError: If max_holders < 1 or max_holders mismatch (SSOT violation)
        """
        if max_holders < 1:
            raise ValueError(f"max_holders must be >= 1, got {max_holders}")

        lock_id = str(uuid.uuid4())
        deadline = time.time() + timeout

        while time.time() < deadline:
            with self._locks_mutex:
                # Check SSOT: if path has existing config, must match
                if path in self._lock_limits:
                    existing_max = self._lock_limits[path]
                    if existing_max != max_holders:
                        raise ValueError(
                            f"max_holders mismatch for {path}: "
                            f"expected {existing_max}, got {max_holders}"
                        )

                holders = self._locks.get(path, [])

                if len(holders) < max_holders:
                    # Has available slot
                    info = _LockInfo(
                        lock_id=lock_id,
                        path=path,
                        acquired_at=time.time(),
                    )
                    if path not in self._locks:
                        self._locks[path] = []
                        self._lock_limits[path] = max_holders  # Set SSOT config
                    self._locks[path].append(info)
                    logger.debug(
                        f"Lock acquired: {path} -> {lock_id} "
                        f"(holders: {len(self._locks[path])}/{max_holders})"
                    )
                    return lock_id

            time.sleep(0.1)

        logger.warning(f"Lock timeout on {path} after {timeout}s")
        return None

    def unlock(self, lock_id: str) -> bool:
        """Release a lock by its ID.

        Args:
            lock_id: Lock ID returned from lock()

        Returns:
            True if released, False if not found
        """
        with self._locks_mutex:
            for path, holders in list(self._locks.items()):
                for i, info in enumerate(holders):
                    if info.lock_id == lock_id:
                        holders.pop(i)
                        logger.debug(
                            f"Lock released: {path} <- {lock_id} (remaining: {len(holders)})"
                        )
                        # Auto-cleanup: if no holders left, remove path and config
                        if not holders:
                            del self._locks[path]
                            del self._lock_limits[path]
                            logger.debug(f"Lock config cleaned up: {path}")
                        return True

        logger.warning(f"Lock not found: {lock_id}")
        return False

    def is_locked(self, path: str) -> bool:
        """Check if a path is currently locked (has any holders)."""
        with self._locks_mutex:
            holders = self._locks.get(path, [])
            return len(holders) > 0
