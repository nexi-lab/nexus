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
from typing import TYPE_CHECKING, Any

from fuse import FuseOSError, Operations

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.fuse.cache import FUSECacheManager

if TYPE_CHECKING:
    from nexus.core.filesystem import NexusFilesystem
    from nexus.fuse.mount import MountMode

logger = logging.getLogger(__name__)


class NexusFUSEOperations(Operations):
    """FUSE operations implementation for Nexus filesystem.

    This class translates FUSE filesystem calls into Nexus filesystem operations,
    providing a POSIX-like interface to Nexus storage.
    """

    def __init__(
        self,
        nexus_fs: NexusFilesystem,
        mode: MountMode,
        auto_parse: bool = False,
        cache_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize FUSE operations.

        Args:
            nexus_fs: Nexus filesystem instance
            mode: Mount mode (binary, text, smart)
            auto_parse: If True, binary files return parsed text directly.
                       If False (default), use .txt/.md suffixes for parsed views.
            cache_config: Optional cache configuration dict with keys:
                         - attr_cache_size: int (default: 1024)
                         - attr_cache_ttl: int (default: 60)
                         - content_cache_size: int (default: 100)
                         - parsed_cache_size: int (default: 50)
                         - enable_metrics: bool (default: False)
        """
        self.nexus_fs = nexus_fs
        self.mode = mode
        self.auto_parse = auto_parse
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
        try:
            # Check cache first
            cached_attrs = self.cache.get_attr(path)
            if cached_attrs is not None:
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
                return self._dir_attrs()

            # Check if file exists
            if not self.nexus_fs.exists(original_path):
                raise FuseOSError(errno.ENOENT)

            # Get file content to determine size
            content = self._get_file_content(original_path, view_type)

            # Return file attributes
            now = time.time()

            # Get uid/gid with Windows compatibility
            try:
                uid = os.getuid()
                gid = os.getgid()
            except AttributeError:
                # Windows doesn't have getuid/getgid
                uid = 0
                gid = 0

            attrs = {
                "st_mode": stat.S_IFREG | 0o644,
                "st_nlink": 1,
                "st_size": len(content),
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
                "st_uid": uid,
                "st_gid": gid,
            }

            # Cache the result
            self.cache.cache_attr(path, attrs)

            return attrs
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except FuseOSError:
            raise
        except Exception as e:
            logger.error(f"Error getting attributes for {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
        try:
            # Standard directory entries
            entries = [".", ".."]

            # Add .raw directory at root
            if path == "/":
                entries.append(".raw")

            # List files in directory (non-recursive) - returns list[str]
            files_raw = self.nexus_fs.list(path, recursive=False, details=False)
            files = files_raw if isinstance(files_raw, list) else []

            for file_path_or_dict in files:
                # Handle both string paths and dict entries
                file_path = (
                    file_path_or_dict
                    if isinstance(file_path_or_dict, str)
                    else str(file_path_or_dict.get("path", ""))
                )

                # Extract just the filename/dirname
                name = file_path.rstrip("/").split("/")[-1]
                if name and name not in entries:
                    entries.append(name)

                    # In smart/text mode, add virtual views for non-text files
                    # But skip if auto_parse is enabled (files are auto-parsed directly)
                    if (
                        not self.auto_parse
                        and self.mode.value != "binary"
                        and not self.nexus_fs.is_directory(file_path)
                        and not name.endswith((".txt", ".md"))
                    ):
                        # Add .txt and .md virtual views
                        entries.append(f"{name}.txt")
                        entries.append(f"{name}.md")

            return entries
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except Exception as e:
            logger.error(f"Error reading directory {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except FuseOSError:
            raise
        except Exception as e:
            logger.error(f"Error opening file {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except Exception as e:
            logger.error(f"Error reading file {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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

            # Read existing content if file exists
            existing_content = b""
            if self.nexus_fs.exists(original_path):
                existing_content = self.nexus_fs.read(original_path)

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
        except Exception as e:
            logger.error(f"Error writing to file {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
        except Exception as e:
            logger.error(f"Error creating file {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except FuseOSError:
            raise
        except Exception as e:
            logger.error(f"Error deleting file {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
        except Exception as e:
            logger.error(f"Error creating directory {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except FuseOSError:
            raise
        except Exception as e:
            logger.error(f"Error removing directory {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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

            # Read and write (Nexus doesn't have native rename)
            content = self.nexus_fs.read(old_path)
            self.nexus_fs.write(new_path, content)
            self.nexus_fs.delete(old_path)

            # Invalidate caches for both old and new paths
            self.cache.invalidate_path(old_path)
            self.cache.invalidate_path(new_path)
            if old != old_path:
                self.cache.invalidate_path(old)
            if new != new_path:
                self.cache.invalidate_path(new)
        except NexusFileNotFoundError:
            raise FuseOSError(errno.ENOENT) from None
        except FuseOSError:
            raise
        except Exception as e:
            logger.error(f"Error renaming {old} to {new}: {e}")
            raise FuseOSError(errno.EIO) from e

    # ============================================================
    # File Attribute Modification
    # ============================================================

    def chmod(self, path: str, mode: int) -> None:
        """Change file mode (permissions).

        Args:
            path: File path
            mode: New mode

        Note:
            This is a no-op as Nexus doesn't support POSIX permissions yet.
        """
        # No-op: Nexus doesn't support POSIX permissions yet
        pass

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Change file ownership.

        Args:
            path: File path
            uid: User ID
            gid: Group ID

        Note:
            This is a no-op as Nexus doesn't support POSIX ownership yet.
        """
        # No-op: Nexus doesn't support POSIX ownership yet
        pass

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
                content = self.nexus_fs.read(original_path)
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
        except Exception as e:
            logger.error(f"Error truncating file {path}: {e}")
            raise FuseOSError(errno.EIO) from e

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
            path: Virtual path (e.g., "/file.pdf.txt" or "/.raw/file.pdf")

        Returns:
            Tuple of (original_path, view_type)
            - original_path: Original file path without virtual suffix
            - view_type: "txt", "md", or None for raw/binary access
        """
        # Handle .raw directory access (always returns binary)
        if path.startswith("/.raw/"):
            original_path = path[5:]  # Remove "/.raw" prefix
            return (original_path, None)

        # In auto_parse mode, check if file should be auto-parsed
        if self.auto_parse and self.nexus_fs.exists(path) and self._should_auto_parse(path):
            return (path, "txt")  # Return parsed text by default

        # Handle .txt and .md virtual views (explicit mode)
        # Only treat as virtual view if:
        # 1. File ends with .txt or .md
        # 2. The file without the extension actually exists
        # 3. The file without the extension doesn't have that extension
        if path.endswith(".txt") and not path.endswith(".txt.txt"):
            base_path = path[:-4]
            # Check if base file exists (this creates a virtual view)
            if self.nexus_fs.exists(base_path):
                return (base_path, "txt")
        elif path.endswith(".md") and not path.endswith(".md.md"):
            base_path = path[:-3]
            # Check if base file exists (this creates a virtual view)
            if self.nexus_fs.exists(base_path):
                return (base_path, "md")

        return (path, None)

    def _should_auto_parse(self, path: str) -> bool:
        """Check if a file should be auto-parsed based on extension.

        Args:
            path: File path

        Returns:
            True if file should be auto-parsed
        """
        # List of binary extensions that should be auto-parsed
        auto_parse_extensions = {
            ".pdf",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
            ".pptx",
            ".ppt",
            ".odt",
            ".ods",
            ".odp",
            ".rtf",
            ".epub",
            # Images (future OCR support)
            # ".png",
            # ".jpg",
            # ".jpeg",
        }

        # Check if file has an auto-parse extension
        return any(path.endswith(ext) for ext in auto_parse_extensions)

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
            content = self.nexus_fs.read(path)
            self.cache.cache_content(path, content)

        # In binary mode or raw access, return as-is
        if self.mode.value == "binary" or view_type is None:
            return content

        # In text mode, try to parse
        if self.mode.value == "text" or (self.mode.value == "smart" and view_type):
            try:
                # Try to decode as text first
                try:
                    decoded_content = content.decode("utf-8").encode("utf-8")
                    # Cache the parsed result
                    self.cache.cache_parsed(path, view_type, decoded_content)
                    return decoded_content
                except UnicodeDecodeError:
                    # Use parser for non-text files
                    from nexus.parsers import ParserRegistry, prepare_content_for_parsing

                    # Prepare content
                    processed_content, effective_path, metadata = prepare_content_for_parsing(
                        content, path
                    )

                    # Get parser
                    registry = ParserRegistry()
                    parser = registry.get_parser(effective_path)

                    if parser:
                        # Parse synchronously (FUSE doesn't support async)
                        import asyncio

                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                        result = loop.run_until_complete(parser.parse(processed_content, metadata))

                        if result and result.text:
                            parsed_content = result.text.encode("utf-8")
                            # Cache the parsed result
                            self.cache.cache_parsed(path, view_type, parsed_content)
                            return parsed_content
            except Exception as e:
                # ParserError is expected for files without parsers (e.g., .bin files)
                # Just fall back to raw content silently
                from nexus.core.exceptions import ParserError

                if isinstance(e, ParserError):
                    logger.debug(f"No parser available for {path}, using raw content")
                else:
                    logger.warning(f"Error parsing file {path}: {e}")

        # Fallback to raw content
        return content

    def _dir_attrs(self) -> dict[str, Any]:
        """Get standard directory attributes.

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

        return {
            "st_mode": stat.S_IFDIR | 0o755,
            "st_nlink": 2,
            "st_size": 4096,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }
