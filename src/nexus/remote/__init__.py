"""Remote Nexus filesystem transport layer.

This module provides the transport and proxy infrastructure for REMOTE
deployment profile. Use ``nexus.connect(config={"profile": "remote", ...})``
to create a remote NexusFS instance.

AgentClient accesses the native daemon's transient runtime registry.

Example:
    >>> import nexus
    >>> nx = nexus.connect(config={"profile": "remote", "url": "http://localhost:2026", "api_key": "sk-xxx"})
    >>> content = nx.sys_read("/workspace/file.txt")
"""

from nexus.contracts.exceptions import (
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteTimeoutError,
)

__all__ = [
    # Error classes
    "RemoteFilesystemError",
    "RemoteConnectionError",
    "RemoteTimeoutError",
]
