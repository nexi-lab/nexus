"""Deprecated: ``volume_local_transport`` was renamed to ``blob_pack_local_transport``.

Phase 8 (refactor/rust-workspace-parallel-layers) renamed the Rust
``VolumeEngine`` type to ``BlobPackEngine`` for clarity (multi-blob
append-only pack format, not a "volume" in the OS sense).  The Python
wrapper class + module were renamed in lock-step:
``VolumeLocalTransport`` → ``BlobPackLocalTransport``,
``volume_local_transport`` → ``blob_pack_local_transport``.

This module re-exports the new class under its old name so existing
``from nexus.backends.transports.volume_local_transport import
VolumeLocalTransport`` imports keep working through one release;
the deprecation alias is removed in the next release.
"""

from __future__ import annotations

import warnings

from nexus.backends.transports.blob_pack_local_transport import (
    TTL_BUCKETS,
    ceil_bucket,
)
from nexus.backends.transports.blob_pack_local_transport import (
    BlobPackLocalTransport as VolumeLocalTransport,
)

warnings.warn(
    "nexus.backends.transports.volume_local_transport is deprecated; "
    "use nexus.backends.transports.blob_pack_local_transport instead. "
    "Will be removed in next release.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["TTL_BUCKETS", "VolumeLocalTransport", "ceil_bucket"]
