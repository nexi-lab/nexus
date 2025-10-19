"""FUSE filesystem mount support for Nexus.

This module provides FUSE (Filesystem in Userspace) mount capability,
allowing Nexus to be mounted as a local filesystem for seamless access
with standard Unix tools.

Features:
- Multiple mount modes (binary, text, smart)
- Virtual file views (.raw/, .txt, .md)
- Standard filesystem operations
- Integration with Nexus parser system

Example usage:
    >>> from nexus import connect
    >>> from nexus.fuse import NexusFUSE, MountMode
    >>>
    >>> # Create Nexus instance
    >>> nx = connect(config={"data_dir": "./nexus-data"})
    >>>
    >>> # Mount to local path
    >>> mount = NexusFUSE(nx, mount_point="/mnt/nexus", mode=MountMode.SMART)
    >>> mount.mount(foreground=False)
    >>>
    >>> # Now use standard tools
    >>> # $ ls /mnt/nexus
    >>> # $ cat /mnt/nexus/workspace/file.pdf.txt
"""

from nexus.fuse.cache import FUSECacheManager
from nexus.fuse.mount import MountMode, NexusFUSE, mount_nexus
from nexus.fuse.operations import NexusFUSEOperations

__all__ = [
    "NexusFUSE",
    "NexusFUSEOperations",
    "MountMode",
    "mount_nexus",
    "FUSECacheManager",
]
