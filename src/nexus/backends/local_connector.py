"""Local filesystem connector - reference mode without data duplication.

This module provides LocalConnectorBackend, a connector that mounts an external local
folder into Nexus's virtual filesystem. Unlike LocalBackend (which uses CAS),
LocalConnectorBackend keeps files in their original location (SSOT - Single Source of Truth).

Key features:
- Zero data duplication (reference mode)
- Full indexing support (semantic search via sync_mount)
- OS-native file watching for change detection (via FileWatcher)
- Direct read/write to original files
- L1-only caching (no L2 PostgreSQL needed since source is local)

Example:
    >>> nx.add_mount(
    ...     mount_point="/mnt/local",
    ...     backend_type="local_connector",
    ...     backend_config={"local_path": "C:/projects"},
    ... )
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.backends.registry import (
    ArgType,
    ConnectionArg,
    register_connector,
)
from nexus.core.exceptions import BackendError
from nexus.core.hash_fast import hash_content
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from nexus.core.context import OperationContext

logger = logging.getLogger(__name__)


# Type alias for casting error responses
_BytesResponse = HandlerResponse[bytes]
_StrResponse = HandlerResponse[str]
_ListDictResponse = HandlerResponse[list[dict[str, Any]]]
_DictResponse = HandlerResponse[dict[str, Any]]
_ListStrResponse = HandlerResponse[list[str]]
_IntResponse = HandlerResponse[int]


@register_connector(
    "local_connector",
    description="Mount local folder into Nexus (reference mode, no copy)",
    category="storage",
)
class LocalConnectorBackend(Backend, CacheConnectorMixin):
    """Local filesystem connector - reference mode without data duplication.

    Mounts an external local folder into Nexus VFS. Files remain in their
    original location (SSOT) - no content duplication to CAS.

    This is different from:
    - LocalBackend: Uses CAS for deduplication (copies content)
    - PassthroughBackend: Uses CAS via pointers (copies content)

    LocalConnectorBackend is similar to GDriveConnector but for local filesystem:
    - Both use backend_path (not content_hash) for path-based access
    - Both support full indexing via sync_mount
    - LocalConnectorBackend doesn't need L2 cache (local disk IS the storage)

    Caching:
    - Uses L1-only mode (l1_only=True) - no L2 PostgreSQL
    - L1 cache stores metadata + disk_path pointing to original file
    - On L1 hit, content is read via mmap from original file
    - On L1 miss, reads file and populates L1 cache

    Storage structure:
        mount_point: /mnt/local-projects
        local_path:  C:\\Users\\user\\projects

        /mnt/local-projects/nexus/README.md
            → C:\\Users\\user\\projects\\nexus\\README.md

    Example:
        >>> backend = LocalConnectorBackend("C:/Users/user/projects")
        >>> content = backend.read_content("", context)  # Uses context.backend_path
    """

    # NexusFS integration: use path-based read (not content_hash based)
    has_virtual_filesystem = True

    # Cache configuration: L1 only, no L2 (PostgreSQL)
    # Local disk is already fast, no need for persistent cache layer
    l1_only = True

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "local_path": ConnectionArg(
            type=ArgType.PATH,
            description="Local folder path to mount",
            required=True,
        ),
        "readonly": ConnectionArg(
            type=ArgType.BOOLEAN,
            description="Mount as read-only",
            required=False,
            default=False,
        ),
        "follow_symlinks": ConnectionArg(
            type=ArgType.BOOLEAN,
            description="Follow symbolic links (default: True)",
            required=False,
            default=True,
        ),
    }

    def __init__(
        self,
        local_path: str | Path,
        readonly: bool = False,
        follow_symlinks: bool = True,
    ) -> None:
        """Initialize LocalConnectorBackend.

        Args:
            local_path: Local folder path to mount
            readonly: If True, write and delete operations will be rejected
            follow_symlinks: If True, follow symbolic links (default: True)

        Raises:
            BackendError: If local_path doesn't exist or is not a directory
        """
        self.local_path = Path(local_path).resolve()
        self.readonly = readonly
        self.follow_symlinks = follow_symlinks
        self._validate_path()

    def _validate_path(self) -> None:
        """Validate local path exists and is a directory.

        Raises:
            BackendError: If path doesn't exist or is not a directory
        """
        if not self.local_path.exists():
            raise BackendError(
                f"Local path does not exist: {self.local_path}",
                backend="local_connector",
                path=str(self.local_path),
            )
        if not self.local_path.is_dir():
            raise BackendError(
                f"Local path is not a directory: {self.local_path}",
                backend="local_connector",
                path=str(self.local_path),
            )

    @property
    def name(self) -> str:
        """Return the backend name."""
        return "local_connector"

    # =========================================================================
    # Path Translation
    # =========================================================================

    def _to_physical(self, virtual_path: str) -> Path:
        """Convert virtual path to physical path with symlink safety.

        Args:
            virtual_path: Path relative to mount point (e.g., "nexus/README.md")

        Returns:
            Absolute physical path on local filesystem

        Raises:
            BackendError: If resolved path escapes mount root (symlink attack)
        """
        clean = virtual_path.lstrip("/")
        physical = self.local_path / clean

        # Resolve symlinks if enabled
        if self.follow_symlinks:
            try:
                resolved = physical.resolve()
            except OSError:
                # Path doesn't exist yet, resolve parent
                resolved = physical.parent.resolve() / physical.name
        else:
            resolved = physical

        # Security: ensure path doesn't escape mount root
        try:
            resolved.relative_to(self.local_path)
        except ValueError as e:
            raise BackendError(
                f"Path escapes mount root: {virtual_path}",
                backend="local_connector",
                path=virtual_path,
            ) from e

        return resolved

    def get_physical_path(self, virtual_path: str) -> Path:
        """Get the physical path for file watching and L1 cache.

        This method is called by:
        - FileWatcher to get the OS path that should be watched for changes
        - CacheConnectorMixin to get disk_path for L1 cache in l1_only mode

        Args:
            virtual_path: Path relative to mount point

        Returns:
            Absolute physical path on local filesystem
        """
        return self._to_physical(virtual_path)

    def get_watch_root(self) -> Path:
        """Get the root path to watch for changes.

        Returns:
            The local_path that was mounted
        """
        return self.local_path

    def get_file_info(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse:
        """
        Get file metadata for delta sync change detection (Issue #1127).

        Returns local file metadata including size, mtime, and inode-based version
        for efficient change detection during incremental sync.

        Args:
            path: Virtual file path (or backend_path from context)
            context: Operation context with optional backend_path

        Returns:
            HandlerResponse with FileInfo containing:
            - size: File size in bytes
            - mtime: Last modification time
            - backend_version: inode:mtime_ns string (changes on content/metadata change)
        """
        from datetime import datetime, timezone

        from nexus.backends.backend import FileInfo

        try:
            # Get backend path
            if context and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            physical = self._to_physical(backend_path)

            if not physical.exists():
                return HandlerResponse.not_found(path, backend_name=self.name)

            # Get file stats
            stat = physical.stat(follow_symlinks=self.follow_symlinks)

            size = stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

            # Build backend_version: inode + mtime_ns for robust change detection
            # This combination detects both content changes (mtime) and file replacement (inode)
            backend_version = f"{stat.st_ino}:{stat.st_mtime_ns}"

            file_info = FileInfo(
                size=size,
                mtime=mtime,
                backend_version=backend_version,
                content_hash=None,  # Not computed to avoid reading file content
            )

            return HandlerResponse.ok(file_info, backend_name=self.name, path=path)

        except FileNotFoundError:
            return HandlerResponse.not_found(path, backend_name=self.name)
        except PermissionError as e:
            return HandlerResponse.error(f"Permission denied: {path} - {e}", backend_name=self.name)
        except OSError as e:
            return HandlerResponse.error(f"Failed to get file info: {e}", backend_name=self.name)

    # =========================================================================
    # Content Operations (with L1 Caching)
    # =========================================================================

    def read_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bytes]:
        """Read file content with L1 caching.

        For LocalConnectorBackend, content_hash is ignored - we use context.backend_path.

        Flow:
        1. Check L1 cache (TTL-based)
        2. If hit, return cached content (mmap from original file)
        3. If miss, read from disk and populate L1 cache

        Args:
            content_hash: Ignored (LocalConnectorBackend uses path-based access)
            context: Operation context with backend_path

        Returns:
            HandlerResponse with file bytes on success, error message on failure
        """
        start_time = time.perf_counter()

        if context is None or not context.backend_path:
            return cast(
                _BytesResponse,
                HandlerResponse.error("LocalConnectorBackend requires context with backend_path"),
            )

        path = context.backend_path
        cache_path = context.virtual_path if context.virtual_path else path

        # Step 1: Check L1 cache
        if self._has_caching():
            with contextlib.suppress(Exception):
                cached = self._read_from_cache(cache_path, original=True)
                if cached and not cached.stale and cached.content_binary:
                    logger.info(f"[LocalConnectorBackend] L1 cache hit: {cache_path}")
                    return HandlerResponse.ok(
                        data=cached.content_binary,
                        execution_time_ms=(time.perf_counter() - start_time) * 1000,
                        backend_name=self.name,
                        path=path,
                    )

        # Step 2: L1 miss - read from disk
        logger.debug(f"[LocalConnectorBackend] L1 cache miss, reading from disk: {path}")
        physical = self._to_physical(path)

        if not physical.exists():
            return cast(
                _BytesResponse,
                HandlerResponse.not_found(
                    path=path,
                    message=f"File not found: {path}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                ),
            )
        if not physical.is_file():
            return cast(_BytesResponse, HandlerResponse.error(f"Not a file: {path}"))

        try:
            content = physical.read_bytes()
        except PermissionError as e:
            return cast(_BytesResponse, HandlerResponse.error(f"Permission denied: {path} - {e}"))
        except OSError as e:
            return cast(_BytesResponse, HandlerResponse.error(f"Read error: {e}"))

        # Step 3: Populate L1 cache for future reads
        if self._has_caching():
            with contextlib.suppress(Exception):
                tenant_id = getattr(context, "tenant_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    tenant_id=tenant_id,
                )

        return HandlerResponse.ok(
            data=content,
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name=self.name,
            path=path,
        )

    def write_content(
        self,
        content: bytes,
        context: OperationContext | None = None,
    ) -> HandlerResponse[str]:
        """Write content directly to local path.

        Unlike CAS-based backends, this writes directly to the file.
        Returns content hash for consistency with other backends.
        Invalidates L1 cache after write.

        Args:
            content: Bytes to write
            context: Operation context with backend_path

        Returns:
            HandlerResponse with content hash (SHA-256) on success
        """
        if self.readonly:
            return cast(_StrResponse, HandlerResponse.error("Backend is read-only"))

        # Get path from context
        write_path = context.backend_path if context else None

        if write_path is None:
            return cast(
                _StrResponse,
                HandlerResponse.error("Path required for local_connector backend"),
            )

        physical = self._to_physical(write_path)

        try:
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_bytes(content)

            # Invalidate/update L1 cache
            cache_path = context.virtual_path if context and context.virtual_path else write_path
            if self._has_caching():
                with contextlib.suppress(Exception):
                    tenant_id = getattr(context, "tenant_id", None) if context else None
                    self._write_to_cache(
                        path=cache_path,
                        content=content,
                        tenant_id=tenant_id,
                    )

            # Return content hash for consistency
            content_hash = hash_content(content)
            return HandlerResponse.ok(data=content_hash)
        except PermissionError as e:
            return cast(
                _StrResponse, HandlerResponse.error(f"Permission denied: {write_path} - {e}")
            )
        except OSError as e:
            return cast(_StrResponse, HandlerResponse.error(f"Write error: {e}"))

    # =========================================================================
    # Directory Operations
    # =========================================================================

    def list_dir(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[str]:
        """List directory contents.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused for local connector)

        Returns:
            List of entry names in the directory
        """
        physical = self._to_physical(path)

        if not physical.exists() or not physical.is_dir():
            return []

        try:
            return sorted(item.name for item in physical.iterdir())
        except (PermissionError, OSError):
            return []

    def list_dir_detailed(
        self,
        path: str = "",
        context: OperationContext | None = None,
    ) -> HandlerResponse[list[dict[str, Any]]]:
        """List directory contents with detailed metadata.

        Args:
            path: Virtual path relative to mount point (default: root)
            context: Operation context (unused for local connector)

        Returns:
            HandlerResponse with list of entry dicts on success
        """
        physical = self._to_physical(path)

        if not physical.exists():
            return cast(
                _ListDictResponse,
                HandlerResponse.not_found(path=path, message=f"Directory not found: {path}"),
            )
        if not physical.is_dir():
            return cast(_ListDictResponse, HandlerResponse.error(f"Not a directory: {path}"))

        try:
            entries = []
            for item in physical.iterdir():
                try:
                    stat = item.stat(follow_symlinks=self.follow_symlinks)
                    is_dir = item.is_dir()
                    entry = {
                        "name": item.name,
                        "type": "directory" if is_dir else "file",
                        "size": stat.st_size if not is_dir else 0,
                        "modified": stat.st_mtime,
                    }
                    entries.append(entry)
                except OSError:
                    # Skip entries we can't stat (broken symlinks, permission issues)
                    logger.debug(f"Skipping unreadable entry: {item}")
                    continue
            return HandlerResponse.ok(data=entries)
        except PermissionError as e:
            return cast(
                _ListDictResponse, HandlerResponse.error(f"Permission denied: {path} - {e}")
            )
        except OSError as e:
            return cast(_ListDictResponse, HandlerResponse.error(f"List error: {e}"))

    def exists(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if path exists.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused)

        Returns:
            True if path exists, False otherwise
        """
        try:
            return self._to_physical(path).exists()
        except BackendError:
            # Path escapes mount root
            return False

    def is_dir(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if path is a directory.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused)

        Returns:
            True if path is a directory, False otherwise
        """
        try:
            return self._to_physical(path).is_dir()
        except BackendError:
            return False

    def delete(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Delete file or empty directory.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused)

        Returns:
            HandlerResponse with None on success, error message on failure
        """
        if self.readonly:
            return HandlerResponse.error("Backend is read-only")

        physical = self._to_physical(path)

        try:
            if physical.is_file():
                physical.unlink()
            elif physical.is_dir():
                physical.rmdir()  # Only empty directories
            else:
                return HandlerResponse.not_found(path=path, message=f"Path not found: {path}")
            return HandlerResponse.ok(data=None)
        except PermissionError as e:
            return HandlerResponse.error(f"Permission denied: {path} - {e}")
        except OSError as e:
            return HandlerResponse.error(f"Delete error: {e}")

    # =========================================================================
    # Backend Interface Methods (for Backend abstract base class)
    # =========================================================================

    def delete_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Delete content by hash - not supported for local_connector.

        LocalConnectorBackend uses path-based access, not content-hash based.
        This method exists for Backend interface compatibility.
        """
        return HandlerResponse.error(
            "delete_content by hash not supported for local_connector. Use delete(path) instead."
        )

    def content_exists(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bool]:
        """Check if content exists by hash - not supported for local_connector."""
        return HandlerResponse.ok(data=False)

    def get_content_size(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[int]:
        """Get content size by hash - not supported for local_connector."""
        return cast(
            _IntResponse,
            HandlerResponse.error("get_content_size by hash not supported for local_connector"),
        )

    def get_ref_count(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[int]:
        """Get reference count by hash - not supported for local_connector."""
        return HandlerResponse.ok(data=0)

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Create a directory.

        Args:
            path: Virtual path relative to mount point
            parents: Create parent directories if needed (always True for local_connector)
            exist_ok: Don't error if directory exists (always True for local_connector)
            context: Operation context (unused)

        Returns:
            HandlerResponse with None on success
        """
        if self.readonly:
            return HandlerResponse.error("Backend is read-only")

        physical = self._to_physical(path)

        try:
            # Always use parents=True, exist_ok=True for simplicity
            physical.mkdir(parents=True, exist_ok=True)
            return HandlerResponse.ok(data=None)
        except PermissionError as e:
            return HandlerResponse.error(f"Permission denied: {path} - {e}")
        except OSError as e:
            return HandlerResponse.error(f"Mkdir error: {e}")

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Remove a directory.

        Args:
            path: Virtual path relative to mount point
            recursive: If True, remove directory and contents (not supported)
            context: Operation context (unused)

        Returns:
            HandlerResponse with None on success
        """
        if recursive:
            return HandlerResponse.error("Recursive rmdir not supported for safety")
        return self.delete(path, context)

    def is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bool]:
        """Check if path is a directory."""
        return HandlerResponse.ok(data=self.is_dir(path, context))

    def rename(
        self,
        old_path: str,
        new_path: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Rename/move a file or directory.

        Args:
            old_path: Current virtual path
            new_path: New virtual path
            context: Operation context (unused)

        Returns:
            HandlerResponse with None on success
        """
        if self.readonly:
            return HandlerResponse.error("Backend is read-only")

        old_physical = self._to_physical(old_path)
        new_physical = self._to_physical(new_path)

        if not old_physical.exists():
            return HandlerResponse.not_found(path=old_path, message=f"Source not found: {old_path}")

        try:
            # Create parent directories for destination if needed
            new_physical.parent.mkdir(parents=True, exist_ok=True)
            old_physical.rename(new_physical)
            return HandlerResponse.ok(data=None)
        except PermissionError as e:
            return HandlerResponse.error(f"Permission denied: {e}")
        except OSError as e:
            return HandlerResponse.error(f"Rename error: {e}")

    def stat(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[dict[str, Any]]:
        """Get file or directory metadata.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused)

        Returns:
            HandlerResponse with stat dict containing size, mtime, ctime, is_dir
        """
        physical = self._to_physical(path)

        if not physical.exists():
            return cast(
                _DictResponse,
                HandlerResponse.not_found(path=path, message=f"Path not found: {path}"),
            )

        try:
            st = physical.stat(follow_symlinks=self.follow_symlinks)
            return HandlerResponse.ok(
                data={
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "ctime": st.st_ctime,
                    "atime": st.st_atime,
                    "is_dir": physical.is_dir(),
                    "is_file": physical.is_file(),
                    "is_symlink": physical.is_symlink(),
                    "mode": st.st_mode,
                }
            )
        except PermissionError as e:
            return cast(_DictResponse, HandlerResponse.error(f"Permission denied: {path} - {e}"))
        except OSError as e:
            return cast(_DictResponse, HandlerResponse.error(f"Stat error: {e}"))

    def glob(
        self,
        pattern: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[list[str]]:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g., "*.txt", "**/*.py")
            context: Operation context (unused)

        Returns:
            HandlerResponse with list of matching paths (relative to mount)
        """
        try:
            matches = []
            for match in self.local_path.glob(pattern):
                # Security: ensure match is within mount root
                try:
                    rel_path = match.relative_to(self.local_path)
                    matches.append(str(rel_path).replace("\\", "/"))
                except ValueError:
                    # Path escapes mount root (shouldn't happen with glob)
                    continue
            return HandlerResponse.ok(data=sorted(matches))
        except PermissionError as e:
            return cast(_ListStrResponse, HandlerResponse.error(f"Permission denied: {e}"))
        except OSError as e:
            return cast(_ListStrResponse, HandlerResponse.error(f"Glob error: {e}"))
