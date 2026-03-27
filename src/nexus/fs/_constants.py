"""Shared constants for the nexus-fs package.

The canonical size limit lives in ``nexus.contracts.constants`` (tier-neutral)
so the kernel can also use it without a layer violation.  This module
re-exports it under the slim-package name for backward compatibility.
"""

from __future__ import annotations

from nexus.contracts.constants import NEXUS_FS_MAX_INMEMORY_SIZE

# Re-export under the name that other nexus-fs modules use.
DEFAULT_MAX_FILE_SIZE = NEXUS_FS_MAX_INMEMORY_SIZE

# Streaming copy chunk size (64 MB).
STREAMING_COPY_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB
