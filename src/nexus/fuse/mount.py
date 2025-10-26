"""FUSE mount management for Nexus filesystem.

This module provides the high-level interface for mounting Nexus as a
FUSE filesystem, including mount mode management and lifecycle control.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from fuse import FUSE

if TYPE_CHECKING:
    from nexus.core.filesystem import NexusFilesystem

from nexus.fuse.operations import NexusFUSEOperations

logger = logging.getLogger(__name__)


class MountMode(Enum):
    """Mount mode for FUSE filesystem.

    - BINARY: Return raw file content (no parsing)
    - TEXT: Parse all files and return text representation
    - SMART: Auto-detect file type and return appropriate format
    """

    BINARY = "binary"
    TEXT = "text"
    SMART = "smart"


class NexusFUSE:
    """FUSE mount manager for Nexus filesystem.

    This class manages the lifecycle of a FUSE mount, including starting,
    stopping, and monitoring the mount.

    Example:
        >>> from nexus import connect
        >>> from nexus.fuse import NexusFUSE, MountMode
        >>>
        >>> nx = connect(config={"data_dir": "./nexus-data"})
        >>> fuse = NexusFUSE(nx, "/mnt/nexus", mode=MountMode.SMART)
        >>> fuse.mount(foreground=False)
        >>> # ... use filesystem ...
        >>> fuse.unmount()
    """

    def __init__(
        self,
        nexus_fs: NexusFilesystem,
        mount_point: str,
        mode: MountMode = MountMode.SMART,
        cache_config: dict[str, int | bool] | None = None,
    ) -> None:
        """Initialize FUSE mount manager.

        Args:
            nexus_fs: Nexus filesystem instance to mount
            mount_point: Local path to mount the filesystem
            mode: Mount mode (binary, text, or smart)
            cache_config: Optional cache configuration dict with keys:
                         - attr_cache_size: int (default: 1024)
                         - attr_cache_ttl: int (default: 60)
                         - content_cache_size: int (default: 100)
                         - parsed_cache_size: int (default: 50)
                         - enable_metrics: bool (default: False)
        """
        self.nexus_fs = nexus_fs
        self.mount_point = Path(mount_point)
        self.mode = mode
        self.cache_config = cache_config
        self.fuse: FUSE | None = None
        self._mount_thread: threading.Thread | None = None
        self._mounted = False

    def mount(
        self,
        foreground: bool = True,
        allow_other: bool = False,
        debug: bool = False,
    ) -> None:
        """Mount the Nexus filesystem.

        Args:
            foreground: If True, run in foreground (blocking); if False, run in background
            allow_other: If True, allow other users to access the mount
            debug: If True, enable FUSE debug output

        Raises:
            RuntimeError: If already mounted or mount fails
            FileNotFoundError: If mount point doesn't exist
        """
        if self._mounted:
            raise RuntimeError("Filesystem is already mounted")

        # Ensure mount point exists
        if not self.mount_point.exists():
            raise FileNotFoundError(f"Mount point does not exist: {self.mount_point}")

        # Check if mount point is a directory
        if not self.mount_point.is_dir():
            raise ValueError(f"Mount point is not a directory: {self.mount_point}")

        # Check if mount point is empty
        if list(self.mount_point.iterdir()):
            logger.warning(f"Mount point is not empty: {self.mount_point}")

        # Create FUSE operations
        operations = NexusFUSEOperations(self.nexus_fs, self.mode, self.cache_config)

        # Build FUSE options
        # Note: Always use foreground=True because we handle backgrounding ourselves via threading
        fuse_options: dict[str, bool | str | int] = {
            "nothreads": False,
            "foreground": True,  # Always run FUSE in foreground mode (within our thread)
            "debug": debug,
        }

        if allow_other:
            fuse_options["allow_other"] = True

        # Add macOS-specific options to prevent Spotlight indexing and reduce overhead
        # These options tell macOS not to index the filesystem and disable extended attributes
        import platform

        if platform.system() == "Darwin":  # macOS
            logger.info("Applying macOS-specific FUSE options to reduce Spotlight indexing")
            fuse_options.update(
                {
                    "volname": "Nexus",  # Custom volume name
                    "noappledouble": True,  # Disable ._* AppleDouble files
                    "noapplexattr": True,  # Disable extended attributes
                    "daemon_timeout": 600,  # Keep daemon alive longer
                    "auto_cache": True,  # Enable automatic kernel caching
                }
            )
            logger.info(f"FUSE options: {fuse_options}")

        # Mount filesystem
        logger.info(f"Mounting Nexus to {self.mount_point} (mode={self.mode.value})")

        if foreground:
            # Run in foreground (blocking)
            self._mounted = True
            try:
                self.fuse = FUSE(
                    operations,
                    str(self.mount_point),
                    **fuse_options,
                )
            finally:
                self._mounted = False
        else:
            # Run in background thread
            def mount_thread() -> None:
                try:
                    self.fuse = FUSE(
                        operations,
                        str(self.mount_point),
                        **fuse_options,
                    )
                except Exception as e:
                    logger.error(f"FUSE mount error: {e}")
                finally:
                    self._mounted = False

            # Use daemon=False so the thread keeps the process alive
            self._mount_thread = threading.Thread(target=mount_thread, daemon=False)
            self._mount_thread.start()
            self._mounted = True

            # Wait a bit to ensure mount succeeds
            import time

            time.sleep(1)

            if not self._mounted:
                raise RuntimeError("Failed to mount filesystem")

            logger.info(f"Mounted Nexus to {self.mount_point} (background)")

    def unmount(self) -> None:
        """Unmount the filesystem.

        Raises:
            RuntimeError: If not mounted
        """
        if not self._mounted:
            raise RuntimeError("Filesystem is not mounted")

        logger.info(f"Unmounting {self.mount_point}")

        # Use platform-specific unmount command
        import platform
        import subprocess

        system = platform.system()

        try:
            if system == "Darwin":  # macOS
                subprocess.run(
                    ["umount", str(self.mount_point)],
                    check=True,
                    capture_output=True,
                )
            elif system == "Linux":
                subprocess.run(
                    ["fusermount", "-u", str(self.mount_point)],
                    check=True,
                    capture_output=True,
                )
            else:
                raise RuntimeError(f"Unsupported platform: {system}")

            self._mounted = False
            logger.info(f"Unmounted {self.mount_point}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to unmount: {e.stderr.decode()}")
            raise RuntimeError(f"Failed to unmount: {e.stderr.decode()}") from e

    def is_mounted(self) -> bool:
        """Check if filesystem is currently mounted.

        Returns:
            True if mounted, False otherwise
        """
        return self._mounted

    def wait(self) -> None:
        """Wait for the mount thread to finish.

        This is useful when running in background mode to keep the
        process alive.
        """
        if self._mount_thread and self._mount_thread.is_alive():
            self._mount_thread.join()

    def __enter__(self) -> NexusFUSE:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: type, exc_val: Exception, exc_tb: object) -> None:
        """Context manager exit."""
        if self._mounted:
            try:
                self.unmount()
            except Exception as e:
                logger.error(f"Error unmounting in context manager: {e}")


def mount_nexus(
    nexus_fs: NexusFilesystem,
    mount_point: str,
    mode: str = "smart",
    foreground: bool = True,
    allow_other: bool = False,
    debug: bool = False,
    cache_config: dict[str, int | bool] | None = None,
) -> NexusFUSE:
    """Convenience function to mount Nexus filesystem.

    Args:
        nexus_fs: Nexus filesystem instance
        mount_point: Local path to mount
        mode: Mount mode ("binary", "text", or "smart")
        foreground: Run in foreground (blocking)
        allow_other: Allow other users to access the mount
        debug: Enable FUSE debug output
        cache_config: Optional cache configuration dict with keys:
                     - attr_cache_size: int (default: 1024)
                     - attr_cache_ttl: int (default: 60)
                     - content_cache_size: int (default: 100)
                     - parsed_cache_size: int (default: 50)
                     - enable_metrics: bool (default: False)

    Returns:
        NexusFUSE instance

    Example:
        >>> from nexus import connect
        >>> from nexus.fuse import mount_nexus
        >>>
        >>> nx = connect(config={"data_dir": "./nexus-data"})
        >>>
        >>> # Mount with virtual parsed views
        >>> fuse = mount_nexus(nx, "/mnt/nexus", mode="smart", foreground=False)
        >>> # cat /mnt/nexus/file.xlsx → binary content
        >>> # cat /mnt/nexus/file_parsed.xlsx.md → parsed markdown
        >>>
        >>> # Custom cache configuration
        >>> cache_config = {
        ...     "attr_cache_size": 2048,
        ...     "attr_cache_ttl": 120,
        ...     "enable_metrics": True
        ... }
        >>> fuse = mount_nexus(nx, "/mnt/nexus", cache_config=cache_config, foreground=False)
    """
    # Parse mode
    mode_enum = MountMode(mode.lower())

    # Create and mount
    fuse = NexusFUSE(nexus_fs, mount_point, mode=mode_enum, cache_config=cache_config)
    fuse.mount(foreground=foreground, allow_other=allow_other, debug=debug)

    return fuse
