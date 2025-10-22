"""Remote Nexus filesystem client.

This module provides a remote client implementation of NexusFilesystem
that connects to a Nexus RPC server over HTTP.
"""

from nexus.remote.client import (
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteNexusFS,
    RemoteTimeoutError,
)

__all__ = [
    "RemoteNexusFS",
    "RemoteFilesystemError",
    "RemoteConnectionError",
    "RemoteTimeoutError",
]
