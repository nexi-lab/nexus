"""Filesystem contracts (legacy re-export).

``NexusFilesystem`` Protocol has been deleted.  The SSOT for the kernel
API is now the Rust ``pub fn sys_*`` methods.  Use ``NexusFS`` directly::

    from nexus.core.nexus_fs import NexusFS
"""

__all__: list[str] = []
