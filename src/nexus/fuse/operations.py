"""FUSE operation handlers for Nexus filesystem.

This module implements the low-level FUSE operations that map filesystem
calls to Nexus filesystem operations.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from typing import TYPE_CHECKING, Any, NoReturn

from fuse import FuseOSError, Operations

from nexus.core.exceptions import NexusFileNotFoundError, NexusPermissionError
from nexus.core.filters import is_os_metadata_file
from nexus.core.virtual_views import (
    get_parsed_content,
    parse_virtual_path,
    should_add_virtual_views,
)
from nexus.fuse.cache import FUSECacheManager

if TYPE_CHECKING:
    from nexus.core.filesystem import NexusFilesystem
    from nexus.fuse.mount import MountMode

# Import remote exceptions for better error handling (may not be available in all contexts)
try:
    from nexus.remote.client import (
        RemoteConnectionError,
        RemoteFilesystemError,
        RemoteTimeoutError,
    )

    HAS_REMOTE_EXCEPTIONS = True
except ImportError:
    HAS_REMOTE_EXCEPTIONS = False
    RemoteConnectionError = None  # type: ignore[misc,assignment]
    RemoteFilesystemError = None  # type: ignore[misc,assignment]
    RemoteTimeoutError = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


def _handle_remote_exception(e: Exception, operation: str, path: str, **context: Any) -> NoReturn:
    """Handle remote-specific exceptions with better error messages.

    Args:
        e: The exception that occurred
        operation: The FUSE operation name (e.g., "READ", "GETATTR")
        path: The file path being accessed
        **context: Additional context to include in log message

    Raises:
        FuseOSError: With appropriate errno based on exception type
    """
    context_str = ", ".join(f"{k}={v}" for k, v in context.items()) if context else ""

    if HAS_REMOTE_EXCEPTIONS:
        if RemoteTimeoutError is not None and isinstance(e, RemoteTimeoutError):
            logger.error(f"[FUSE-{operation}] Timeout: {path} - {e} ({context_str})")
            raise FuseOSError(errno.ETIMEDOUT) from e
        if RemoteConnectionError is not None and isinstance(e, RemoteConnectionError):
            logger.error(f"[FUSE-{operation}] Connection error: {path} - {e}")
            raise FuseOSError(errno.ECONNREFUSED) from e
        if RemoteFilesystemError is not None and isinstance(e, RemoteFilesystemError):
            logger.error(f"[FUSE-{operation}] Remote error: {path} - {e}")
            raise FuseOSError(errno.EIO) from e

    # Log with stack trace for debugging unexpected errors
    logger.exception(f"[FUSE-{operation}] Unexpected error: {path} ({context_str})")
    raise FuseOSError(errno.EIO) from e


class NexusFUSEOperations(Operations):
    """FUSE operations implementation for Nexus filesystem.

    This class translates FUSE filesystem calls into Nexus filesystem operations,
    providing a POSIX-like interface to Nexus storage.
    """

    def __init__(
        self,
        nexus_fs: NexusFilesystem,
        mode: MountMode,
        cache_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize FUSE operations.

        Args:
            nexus_fs: Nexus filesystem instance
            mode: Mount mode (binary, text, smart)
            cache_config: Optional cache configuration dict with keys:
                         - attr_cache_size: int (default: 1024)
                         - attr_cache_ttl: int (default: 60)
                         - content_cache_size: int (default: 100)
                         - parsed_cache_size: int (default: 50)
                         - enable_metrics: bool (default: False)
        """
        self.nexus_fs = nexus_fs
        self.mode = mode
        self.fd_counter = 0
        self.open_files: dict[int, dict[str, Any]] = {}

        # Initialize cache manager
        cache_config = cache_config or {}
        self.cache = FUSECacheManager(
            attr_cache_size=cache_config.get("attr_cache_size", 1024),
            attr_cache_ttl=cache_config.get("attr_cache_ttl", 60),
            content_cache_size=cache_config.get("content_cache_size", 100),
            parsed_cache_size=cache_config.get("parsed_cache_size", 50),
            enable_metrics=cache_config.get("enable_metrics", False),
        )

    # ============================================================
    # Filesystem Metadata Operations
    # ============================================================

    def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:  # noqa: ARG002
        """Get file attributes.

        Args:
            path: Virtual file path
            fh: Optional file handle (unused)

        Returns:
            Dictionary with file attributes (st_mode, st_size, st_mtime, etc.)

        Raises:
            FuseOSError: If file not found
        """
        import time

        start_time = time.time()
        try:
            # Check cache first
            cached_attrs = self.cache.get_attr(path)
            if cached_attrs is not None:
                elapsed = time.time() - start_time
                if elapsed > 0.001:  # Only log if > 1ms
                    logger.debug(f"[FUSE-PERF] getattr CACHED: path={path}, {elapsed:.3f}s")
                return cached_attrs

            # Handle virtual views (.raw, .txt, .md)
            original_path, view_type = self._parse_virtual_path(path)

            # Special case: root directory always exists
            if original_path == "/":
                return self._dir_attrs()

            # Check if it's the .raw directory itself
            if path == "/.raw":
                return self._dir_attrs()

            # Check if it's a directory
            if self.nexus_fs.is_directory(original_path):
                # Get directory metadata for permissions
                metadata = self._get_metadata(original_path)
                return self._dir_attrs(metadata)

            # Check if file exists
            if not self.nexus_fs.exists(original_path):
                raise FuseOSError(errno.ENOENT)

            # Get file metadata (includes size, permissions, etc.)
            metadata = self._get_metadata(original_path)

            # Get file size efficiently
            # Priority: 1) Use metadata.size if available, 2) Fetch content as fallback
            if view_type and view_type != "raw":
                # Special view - need to fetch content for accurate size
                content = self._get_file_content(original_path, view_type)
                file_size = len(content)
            elif metadata:
                # Try to get size from metadata (handles both dict and object)
                meta_size = (
                    metadata.get("size")
                    if isinstance(metadata, dict)
                    else getattr(metadata, "size", 0)
                )
                if meta_size and meta_size > 0:
                    file_size = meta_size
                else:
                    # Fallback: fetch content to get size
                    content = self._get_file_content(original_path, None)
                    file_size = len(content)
            else:
                # No metadata: fetch content to get size (for backward compatibility)
                content = self._get_file_content(original_path, None)
                file_size = len(content)

            # Return file attributes
            now = time.time()

            # Map owner/group to uid/gid (Unix-only)
            # Default to current user if not set or on Windows
            try:
                uid = os.getuid()
                gid = os.getgid()
            except AttributeError:
                # Windows doesn't have getuid/getgid
                uid = 0
                gid = 0

            # Get permission mode from metadata (default to 0o644)
            file_mode = 0o644
            if metadata and metadata.mode is not None:
                file_mode = metadata.mode

            # Try to map owner/group to uid/gid
            if metadata:
                try:
                    import grp
                    import pwd

                    if metadata.owner:
                        try:
                            uid = pwd.getpwnam(metadata.owner).pw_uid
                        except KeyError:
                            # Username not found, try as numeric
                            import contextlib

                            with contextlib.suppress(ValueError):
                                uid = int(metadata.owner)

                    if metadata.group:
                        try:
                            gid = grp.getgrnam(metadata.group).gr_gid
                        except KeyError:
                            # Group name not found, try as numeric
                            import contextlib

                            with contextlib.suppress(ValueError):
                                gid = int(metadata.group)

                except (ModuleNotFoundError, AttributeError):
                    # Windows doesn't have pwd/grp - use defaults
                    pass

            attrs = {
                "st_mode": stat.S_IFREG | file_mode,
                "st_nlink": 1,
                "st_size": file_size,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
                "st_uid": uid,
                "st_gid": gid,
            }

            # Cache the result
            self.cache.cache_attr(path, attrs)

            elapsed = time.time() - start_time
            if elapsed > 0.01:  # Log if >10ms
                logger.info(f"[FUSE-PERF] getattr UNCACHED: path={path}, {elapsed:.3f}s")
            return attrs
        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-GETATTR] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "GETATTR", path)

    def readdir(self, path: str, fh: int | None = None) -> list[str]:  # noqa: ARG002
        """Read directory contents.

        Args:
            path: Directory path
            fh: Optional file handle (unused)

        Returns:
            List of file/directory names in the directory

        Raises:
            FuseOSError: If directory not found
        """
        import time

        start_time = time.time()
        logger.info(f"[FUSE-PERF] readdir START: path={path}")

        try:
            # Standard directory entries
            entries = [".", ".."]

            # At root level, add .raw directory
            if path == "/":
                entries.append(".raw")

            # List files in directory (non-recursive) - returns list[dict] with details
            # Using details=True gets directory status in bulk, avoiding individual is_directory() calls
            list_start = time.time()
            files_raw = self.nexus_fs.list(path, recursive=False, details=True)
            list_elapsed = time.time() - list_start
            files = files_raw if isinstance(files_raw, list) else []
            logger.info(
                f"[FUSE-PERF] readdir list() took {list_elapsed:.3f}s, returned {len(files)} items"
            )

            for file_info in files:
                # Handle both string paths and dict entries
                if isinstance(file_info, str):
                    # Fallback for backends that don't support details
                    file_path = file_info
                    is_dir = self.nexus_fs.is_directory(file_path)
                else:
                    file_path = str(file_info.get("path", ""))
                    is_dir = file_info.get("is_directory", False)

                    # Pre-cache attributes for this file to avoid N+1 queries in getattr()
                    # This eliminates redundant is_directory() and get_metadata() RPC calls
                    # when the OS calls getattr() on each file after readdir()
                    self._cache_file_attrs_from_list(file_path, file_info, is_dir)

                # Extract just the filename/dirname
                name = file_path.rstrip("/").split("/")[-1]
                if name and name not in entries:
                    # Filter out OS metadata files (._*, .DS_Store, etc.)
                    if is_os_metadata_file(name):
                        continue

                    entries.append(name)

                    # In smart/text mode, add virtual views for non-text files (not directories)
                    if (
                        self.mode.value != "binary"
                        and should_add_virtual_views(name)
                        and not is_dir
                    ):
                        # Add _parsed.{ext}.md virtual view
                        # e.g., "file.xlsx" → "file_parsed.xlsx.md"
                        last_dot = name.rfind(".")
                        if last_dot != -1:
                            base_name = name[:last_dot]
                            extension = name[last_dot:]
                            parsed_name = f"{base_name}_parsed{extension}.md"
                            entries.append(parsed_name)

            # Final filter to remove any OS metadata that might have slipped through
            entries = [e for e in entries if not is_os_metadata_file(e)]

            total_elapsed = time.time() - start_time
            logger.info(
                f"[FUSE-PERF] readdir DONE: path={path}, {len(entries)} entries, {total_elapsed:.3f}s total"
            )
            return entries
        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-READDIR] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "READDIR", path)

    # ============================================================
    # File I/O Operations
    # ============================================================

    def open(self, path: str, flags: int) -> int:
        """Open a file.

        Args:
            path: File path
            flags: Open flags (O_RDONLY, O_WRONLY, O_RDWR, etc.)

        Returns:
            File descriptor (integer handle)

        Raises:
            FuseOSError: If file not found or access denied
        """
        try:
            # Parse virtual path
            original_path, view_type = self._parse_virtual_path(path)

            # Check if file exists
            if not self.nexus_fs.exists(original_path):
                raise FuseOSError(errno.ENOENT)

            # Generate file descriptor
            self.fd_counter += 1
            fd = self.fd_counter

            # Store file info
            self.open_files[fd] = {
                "path": original_path,
                "view_type": view_type,
                "flags": flags,
            }

            return fd
        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-OPEN] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "OPEN", path, flags=flags)

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        """Read file content.

        Args:
            path: File path
            size: Number of bytes to read
            offset: Offset in file to start reading
            fh: File descriptor

        Returns:
            File content bytes

        Raises:
            FuseOSError: If file not found or read error
        """
        try:
            # Get file info from handle
            file_info = self.open_files.get(fh)
            if not file_info:
                raise FuseOSError(errno.EBADF)

            # Get file content
            content = self._get_file_content(file_info["path"], file_info["view_type"])

            # Return requested slice
            return content[offset : offset + size]
        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            logger.error(f"[FUSE-READ] File not found: {path}")
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-READ] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "READ", path, fh=fh, size=size, offset=offset)

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        """Write file content.

        Args:
            path: File path
            data: Data to write
            offset: Offset in file to start writing
            fh: File descriptor

        Returns:
            Number of bytes written

        Raises:
            FuseOSError: If write fails or path is read-only
        """
        try:
            # Get file info from handle
            file_info = self.open_files.get(fh)
            if not file_info:
                raise FuseOSError(errno.EBADF)

            # Don't allow writes to virtual views
            if file_info["view_type"]:
                raise FuseOSError(errno.EROFS)

            original_path = file_info["path"]

            # Block writes to OS metadata files
            basename = original_path.split("/")[-1]
            if is_os_metadata_file(basename):
                logger.debug(f"Blocked write to OS metadata file: {original_path}")
                raise FuseOSError(errno.EPERM)  # Permission denied

            # Read existing content if file exists
            existing_content = b""
            if self.nexus_fs.exists(original_path):
                raw_content = self.nexus_fs.read(original_path)
                # Type narrowing: when return_metadata=False (default), result is bytes
                assert isinstance(raw_content, bytes), "Expected bytes from read()"
                existing_content = raw_content

            # Handle offset writes
            if offset > len(existing_content):
                # Pad with zeros
                existing_content += b"\x00" * (offset - len(existing_content))

            # Combine content
            new_content = existing_content[:offset] + data + existing_content[offset + len(data) :]

            # Write to Nexus
            self.nexus_fs.write(original_path, new_content)

            # Invalidate caches for this path
            self.cache.invalidate_path(original_path)
            if path != original_path:
                self.cache.invalidate_path(path)

            return len(data)
        except FuseOSError:
            raise
        except NexusPermissionError as e:
            logger.error(f"[FUSE-WRITE] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "WRITE", path, offset=offset, data_len=len(data))

    def release(self, path: str, fh: int) -> None:  # noqa: ARG002
        """Release (close) a file.

        Args:
            path: File path
            fh: File descriptor
        """
        # Remove from open files
        self.open_files.pop(fh, None)

    # ============================================================
    # File/Directory Creation and Deletion
    # ============================================================

    def create(self, path: str, mode: int, fi: Any = None) -> int:  # noqa: ARG002
        """Create a new file.

        Args:
            path: File path to create
            mode: File mode (permissions)
            fi: File info (unused)

        Returns:
            File descriptor

        Raises:
            FuseOSError: If creation fails
        """
        try:
            # Block creation of OS metadata files
            basename = path.split("/")[-1]
            if is_os_metadata_file(basename):
                logger.debug(f"Blocked creation of OS metadata file: {path}")
                raise FuseOSError(errno.EPERM)  # Permission denied

            # Parse virtual path (reject virtual views)
            original_path, view_type = self._parse_virtual_path(path)
            if view_type:
                raise FuseOSError(errno.EROFS)

            # Create empty file
            self.nexus_fs.write(original_path, b"")

            # Invalidate caches for this path (in case it existed before)
            self.cache.invalidate_path(original_path)
            if path != original_path:
                self.cache.invalidate_path(path)

            # Generate file descriptor
            self.fd_counter += 1
            fd = self.fd_counter

            # Store file info
            self.open_files[fd] = {
                "path": original_path,
                "view_type": None,
                "flags": os.O_RDWR,
            }

            return fd
        except FuseOSError:
            raise
        except NexusPermissionError as e:
            logger.error(f"[FUSE-CREATE] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "CREATE", path)

    def unlink(self, path: str) -> None:
        """Delete a file.

        Args:
            path: File path to delete

        Raises:
            FuseOSError: If deletion fails or file is read-only
        """
        try:
            # Parse virtual path (reject virtual views)
            original_path, view_type = self._parse_virtual_path(path)
            if view_type:
                raise FuseOSError(errno.EROFS)

            # Delete file
            self.nexus_fs.delete(original_path)

            # Invalidate caches for this path
            self.cache.invalidate_path(original_path)
            if path != original_path:
                self.cache.invalidate_path(path)
        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-UNLINK] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "UNLINK", path)

    def mkdir(self, path: str, mode: int) -> None:  # noqa: ARG002
        """Create a directory.

        Args:
            path: Directory path to create
            mode: Directory mode (permissions)

        Raises:
            FuseOSError: If creation fails
        """
        try:
            # Don't allow creating directories in .raw
            if path.startswith("/.raw/"):
                raise FuseOSError(errno.EROFS)

            self.nexus_fs.mkdir(path, parents=True, exist_ok=True)
        except FuseOSError:
            raise
        except NexusPermissionError as e:
            logger.error(f"[FUSE-MKDIR] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "MKDIR", path)

    def rmdir(self, path: str) -> None:
        """Remove a directory.

        Args:
            path: Directory path to remove

        Raises:
            FuseOSError: If deletion fails or directory is not empty
        """
        try:
            # Don't allow removing .raw directory
            if path == "/.raw":
                raise FuseOSError(errno.EROFS)

            self.nexus_fs.rmdir(path, recursive=False)
        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-RMDIR] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "RMDIR", path)

    def rename(self, old: str, new: str) -> None:
        """Rename/move a file or directory.

        Args:
            old: Current path
            new: New path

        Raises:
            FuseOSError: If rename fails
        """
        try:
            # Parse virtual paths (reject virtual views)
            old_path, old_view = self._parse_virtual_path(old)
            new_path, new_view = self._parse_virtual_path(new)

            if old_view or new_view:
                raise FuseOSError(errno.EROFS)

            # Don't allow renaming in/out of .raw
            if old.startswith("/.raw/") or new.startswith("/.raw/"):
                raise FuseOSError(errno.EROFS)

            # Check if destination already exists - error out to prevent overwriting
            if self.nexus_fs.exists(new_path):
                logger.error(f"Destination {new_path} already exists")
                raise FuseOSError(errno.EEXIST)

            # Check if source is a directory and handle recursively
            if self.nexus_fs.is_directory(old_path):
                # Handle directory rename/move
                logger.debug(f"Renaming directory {old_path} to {new_path}")

                # Create destination directory explicitly to ensure it shows up
                try:
                    self.nexus_fs.mkdir(new_path, parents=True, exist_ok=True)
                except Exception as e:
                    logger.debug(f"mkdir {new_path} failed (may already exist): {e}")

                # List all files recursively
                files = self.nexus_fs.list(old_path, recursive=True, details=True)

                # Move all files (not directories, as they're implicit in Nexus)
                for file_info in files:
                    # Type guard: ensure file_info is a dict (details=True returns dicts)
                    if not isinstance(file_info, dict):
                        continue
                    if not file_info.get("is_directory", False):
                        src_file = file_info["path"]
                        # Replace old path prefix with new path prefix
                        dest_file = src_file.replace(old_path, new_path, 1)

                        logger.debug(f"  Moving file {src_file} to {dest_file}")

                        # Metadata-only rename - instant, no content copy!
                        self.nexus_fs.rename(src_file, dest_file)

                # Delete source directory recursively
                logger.debug(f"Removing source directory {old_path}")
                self.nexus_fs.rmdir(old_path, recursive=True)
            else:
                # Handle file rename/move using metadata-only operation
                logger.debug(f"Renaming file {old_path} to {new_path}")
                # Metadata-only rename - instant, no content copy!
                self.nexus_fs.rename(old_path, new_path)

            # Invalidate caches for both old and new paths
            self.cache.invalidate_path(old_path)
            self.cache.invalidate_path(new_path)

            # Also invalidate parent directories to update listings
            old_parent = old_path.rsplit("/", 1)[0] or "/"
            new_parent = new_path.rsplit("/", 1)[0] or "/"
            self.cache.invalidate_path(old_parent)
            if old_parent != new_parent:
                self.cache.invalidate_path(new_parent)
                # Also invalidate grandparent of destination to show new subdirectories
                new_grandparent = new_parent.rsplit("/", 1)[0] or "/"
                if new_grandparent != new_parent:
                    self.cache.invalidate_path(new_grandparent)
            if old != old_path:
                self.cache.invalidate_path(old)
            if new != new_path:
                self.cache.invalidate_path(new)
        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-RENAME] Permission denied: {old} -> {new} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "RENAME", old, new_path=new)

    # ============================================================
    # File Attribute Modification
    # ============================================================

    def chmod(self, path: str, mode: int) -> None:
        """Change file mode (permissions).

        Args:
            path: File path
            mode: New mode (POSIX permission bits)

        Raises:
            FuseOSError: If chmod fails
        """
        try:
            # Parse virtual path (reject virtual views)
            original_path, view_type = self._parse_virtual_path(path)
            if view_type:
                raise FuseOSError(errno.EROFS)

            # Extract just the permission bits (mask off file type bits)
            permission_bits = mode & 0o777

            # Call Nexus chmod
            self.nexus_fs.chmod(original_path, permission_bits)  # type: ignore[attr-defined]

            # Invalidate caches for this path
            self.cache.invalidate_path(original_path)
            if path != original_path:
                self.cache.invalidate_path(path)

        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-CHMOD] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "CHMOD", path, mode=oct(mode))

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Change file ownership.

        Args:
            path: File path
            uid: User ID
            gid: Group ID

        Raises:
            FuseOSError: If chown fails

        Note:
            On Unix systems, this maps uid/gid to usernames using pwd/grp modules.
            On Windows, this is a no-op as Windows doesn't have uid/gid.
        """
        try:
            # Parse virtual path (reject virtual views)
            original_path, view_type = self._parse_virtual_path(path)
            if view_type:
                raise FuseOSError(errno.EROFS)

            # Map uid/gid to usernames (Unix-only)
            try:
                import grp
                import pwd

                # Check file exists (remote filesystems may not have metadata access)
                if not self.nexus_fs.exists(original_path):
                    raise FuseOSError(errno.ENOENT)

                # Map uid to username (if uid != -1, which means "don't change")
                if uid != -1:
                    try:
                        owner = pwd.getpwuid(uid).pw_name
                        self.nexus_fs.chown(original_path, owner)  # type: ignore[attr-defined]
                    except KeyError:
                        # uid not found, use numeric string
                        owner = str(uid)
                        self.nexus_fs.chown(original_path, owner)  # type: ignore[attr-defined]

                # Map gid to group name (if gid != -1, which means "don't change")
                if gid != -1:
                    try:
                        group = grp.getgrgid(gid).gr_name
                        self.nexus_fs.chgrp(original_path, group)  # type: ignore[attr-defined]
                    except KeyError:
                        # gid not found, use numeric string
                        group = str(gid)
                        self.nexus_fs.chgrp(original_path, group)  # type: ignore[attr-defined]

                # Invalidate caches for this path
                self.cache.invalidate_path(original_path)
                if path != original_path:
                    self.cache.invalidate_path(path)

            except (ModuleNotFoundError, AttributeError):
                # Windows doesn't have pwd/grp modules - silently ignore
                pass

        except FuseOSError:
            raise
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except NexusPermissionError as e:
            logger.error(f"[FUSE-CHOWN] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "CHOWN", path, uid=uid, gid=gid)

    def truncate(self, path: str, length: int, fh: int | None = None) -> None:  # noqa: ARG002
        """Truncate file to specified length.

        Args:
            path: File path
            length: New file size
            fh: Optional file handle

        Raises:
            FuseOSError: If truncate fails
        """
        try:
            # Parse virtual path (reject virtual views)
            original_path, view_type = self._parse_virtual_path(path)
            if view_type:
                raise FuseOSError(errno.EROFS)

            # Read existing content
            if self.nexus_fs.exists(original_path):
                raw_content = self.nexus_fs.read(original_path)
                # Type narrowing: when return_metadata=False (default), result is bytes
                assert isinstance(raw_content, bytes), "Expected bytes from read()"
                content = raw_content
            else:
                content = b""

            # Truncate or pad
            if length < len(content):
                content = content[:length]
            else:
                content += b"\x00" * (length - len(content))

            # Write back
            self.nexus_fs.write(original_path, content)

            # Invalidate caches for this path
            self.cache.invalidate_path(original_path)
            if path != original_path:
                self.cache.invalidate_path(path)
        except FuseOSError:
            raise
        except NexusPermissionError as e:
            logger.error(f"[FUSE-TRUNCATE] Permission denied: {path} - {e}")
            raise FuseOSError(errno.EACCES) from e
        except Exception as e:
            _handle_remote_exception(e, "TRUNCATE", path, length=length)

    def utimens(self, path: str, times: tuple[float, float] | None = None) -> None:
        """Update file access and modification times.

        Args:
            path: File path
            times: Tuple of (atime, mtime) or None for current time

        Note:
            This is a no-op as Nexus manages timestamps internally.
        """
        # No-op: Nexus manages timestamps internally
        pass

    # ============================================================
    # Helper Methods
    # ============================================================

    def _parse_virtual_path(self, path: str) -> tuple[str, str | None]:
        """Parse virtual path to extract original path and view type.

        Args:
            path: Virtual path (e.g., "/file_parsed.xlsx.md" or "/.raw/file.xlsx")

        Returns:
            Tuple of (original_path, view_type)
            - original_path: Original file path without virtual suffix
            - view_type: "md" or None for raw/binary access
        """
        # Handle .raw directory access (always returns binary)
        if path.startswith("/.raw/"):
            original_path = path[5:]  # Remove "/.raw" prefix
            return (original_path, None)

        # Use shared virtual view logic
        return parse_virtual_path(path, self.nexus_fs.exists)

    def _get_file_content(self, path: str, view_type: str | None) -> bytes:
        """Get file content with appropriate view transformation.

        Args:
            path: Original file path
            view_type: View type ("txt", "md", or None for binary)

        Returns:
            File content as bytes
        """
        # Check parsed cache first if we need parsing
        if view_type and (self.mode.value == "text" or self.mode.value == "smart"):
            cached_parsed = self.cache.get_parsed(path, view_type)
            if cached_parsed is not None:
                return cached_parsed

        # Check content cache for raw content
        content = self.cache.get_content(path)
        if content is None:
            # Read from filesystem and cache
            raw_content = self.nexus_fs.read(path)
            # Type narrowing: when return_metadata=False (default), result is bytes
            assert isinstance(raw_content, bytes), "Expected bytes from read()"
            content = raw_content
            self.cache.cache_content(path, content)

        # In binary mode or raw access, return as-is
        if self.mode.value == "binary" or view_type is None:
            return content

        # In text mode, try to parse
        if self.mode.value == "text" or (self.mode.value == "smart" and view_type):
            # Use shared parsing logic
            parsed_content = get_parsed_content(content, path, view_type or "txt")
            # Cache the parsed result
            self.cache.cache_parsed(path, view_type, parsed_content)
            return parsed_content

        # Fallback to raw content
        return content

    def _get_metadata(self, path: str) -> Any:
        """Get file/directory metadata from filesystem.

        Works with both local filesystems (direct metadata access) and
        remote filesystems (RPC get_metadata call).

        Args:
            path: File or directory path

        Returns:
            Metadata object/dict or None if not available
        """
        # Try get_metadata method first (for RemoteNexusFS)
        if hasattr(self.nexus_fs, "get_metadata"):
            metadata_dict = self.nexus_fs.get_metadata(path)
            if metadata_dict:
                # Convert dict to simple object with attributes
                class MetadataObj:
                    def __init__(self, d: dict[str, Any]):
                        self.path = d.get("path")
                        self.size = d.get("size")
                        self.owner = d.get("owner")
                        self.group = d.get("group")
                        self.mode = d.get("mode")
                        self.is_directory = d.get("is_directory")

                return MetadataObj(metadata_dict)
            return None

        # Fall back to direct metadata access (for local NexusFS)
        if hasattr(self.nexus_fs, "metadata"):
            return self.nexus_fs.metadata.get(path)

        return None

    def _cache_file_attrs_from_list(
        self, file_path: str, file_info: dict[str, Any], is_dir: bool
    ) -> None:
        """Cache file attributes from list() results to avoid N+1 queries.

        When readdir() fetches directory contents with details=True, it gets
        is_directory and size info for all files in one RPC call. This method
        caches that data so subsequent getattr() calls don't need additional
        RPC calls for is_directory() and get_metadata().

        Args:
            file_path: Full path to the file
            file_info: File info dict from list(details=True)
            is_dir: Whether this is a directory
        """
        now = time.time()

        # Get uid/gid with Windows compatibility
        try:
            uid = os.getuid()
            gid = os.getgid()
        except AttributeError:
            # Windows doesn't have getuid/getgid
            uid = 0
            gid = 0

        # Build attributes based on whether it's a directory or file
        if is_dir:
            attrs = {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_nlink": 2,
                "st_size": 4096,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
                "st_uid": uid,
                "st_gid": gid,
            }
        else:
            # Get file size from list() results if available
            file_size = file_info.get("size", 0)

            attrs = {
                "st_mode": stat.S_IFREG | 0o644,
                "st_nlink": 1,
                "st_size": file_size,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
                "st_uid": uid,
                "st_gid": gid,
            }

        # Cache the attributes (uses existing 60s TTL)
        self.cache.cache_attr(file_path, attrs)

    def _dir_attrs(self, metadata: Any = None) -> dict[str, Any]:
        """Get standard directory attributes.

        Args:
            metadata: Optional FileMetadata object for permission information

        Returns:
            Dictionary with directory attributes
        """
        now = time.time()

        # Get uid/gid with Windows compatibility
        try:
            uid = os.getuid()
            gid = os.getgid()
        except AttributeError:
            # Windows doesn't have getuid/getgid
            uid = 0
            gid = 0

        # Get permission mode from metadata (default to 0o755 for directories)
        dir_mode = 0o755
        if metadata and hasattr(metadata, "mode") and metadata.mode is not None:
            dir_mode = metadata.mode

        # Try to map owner/group to uid/gid
        if metadata:
            try:
                import grp
                import pwd

                if hasattr(metadata, "owner") and metadata.owner:
                    try:
                        uid = pwd.getpwnam(metadata.owner).pw_uid
                    except KeyError:
                        # Username not found, try as numeric
                        import contextlib

                        with contextlib.suppress(ValueError):
                            uid = int(metadata.owner)

                if hasattr(metadata, "group") and metadata.group:
                    try:
                        gid = grp.getgrnam(metadata.group).gr_gid
                    except KeyError:
                        # Group name not found, try as numeric
                        import contextlib

                        with contextlib.suppress(ValueError):
                            gid = int(metadata.group)

            except (ModuleNotFoundError, AttributeError):
                # Windows doesn't have pwd/grp - use defaults
                pass

        return {
            "st_mode": stat.S_IFDIR | dir_mode,
            "st_nlink": 2,
            "st_size": 4096,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }
