"""Raft consensus client for Nexus.

This module provides Python clients to communicate with Rust Raft nodes
for metadata and lock operations.

Two interfaces are provided:
1. gRPC (RaftClient) - For RemoteNexusFS to access Raft cluster (remote mode)
2. PyO3 FFI (LocalRaft) - For NexusFS on same box as Raft node (local mode, faster)

Architecture:
    Zone internal (local):  NexusFS -> PyO3 FFI -> Rust (nexus_raft) -> sled (~5μs)
    Zone external (remote): RemoteNexusFS -> gRPC -> Rust Raft cluster (~200μs)

Example (gRPC - for RemoteNexusFS):
    from nexus.raft import RaftClient

    async with RaftClient("10.0.0.2:2026") as client:
        await client.put_metadata(file_metadata)
        metadata = await client.get_metadata("/path/to/file")
        await client.acquire_lock("resource", holder_id="agent-123")

Example (PyO3 - for NexusFS local mode):
    from nexus.raft import LocalRaft

    raft = LocalRaft("/var/lib/nexus/raft-zone1")
    raft.set_metadata("/path/to/file", metadata_bytes)
    metadata = raft.get_metadata("/path/to/file")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# gRPC client for remote Raft access (used by RemoteNexusFS)
from nexus.raft.client import (
    LockInfo as RemoteLockInfo,  # Renamed to avoid conflict with PyO3 LockInfo
)
from nexus.raft.client import (
    LockResult,
    RaftClient,
    RaftClientConfig,
    RaftClientPool,
    RaftError,
    RaftNotLeaderError,
)

# PyO3 FFI for local Raft nodes (built by maturin)
# Import from nexus._nexus_raft (consistent with nexus._nexus_fast)
if TYPE_CHECKING:
    from nexus._nexus_raft import (
        HolderInfo as HolderInfo,
    )
    from nexus._nexus_raft import (
        LocalRaft as LocalRaft,
    )
    from nexus._nexus_raft import (
        LockInfo as LockInfo,
    )
    from nexus._nexus_raft import (
        LockState as LockState,
    )

try:
    from nexus._nexus_raft import (
        HolderInfo,
        LocalRaft,
        LockInfo,
        LockState,
    )

    _HAS_LOCAL_RAFT = True
except ImportError:
    # Native module not available - maturin build required
    # Run: maturin develop -m rust/nexus_raft/Cargo.toml --features python
    _HAS_LOCAL_RAFT = False
    LocalRaft = None
    LockState = None
    LockInfo = None
    HolderInfo = None
    logger.debug(
        "LocalRaft not available. Install with: "
        "maturin develop -m rust/nexus_raft/Cargo.toml --features python"
    )


def require_local_raft() -> None:
    """Require LocalRaft to be available.

    Call this before using LocalRaft to get a clear error message.

    Raises:
        RuntimeError: If LocalRaft is not available
    """
    if not _HAS_LOCAL_RAFT:
        raise RuntimeError(
            "LocalRaft is not available. Build with:\n"
            "  maturin develop -m rust/nexus_raft/Cargo.toml --features python\n"
            "Or install the pre-built wheel:\n"
            "  pip install nexus-ai-fs"
        )


__all__ = [
    # gRPC client (remote - for RemoteNexusFS)
    "RaftClient",
    "RaftClientPool",
    "RaftClientConfig",
    "RaftError",
    "RaftNotLeaderError",
    "LockResult",
    "RemoteLockInfo",
    # PyO3 FFI (local - for NexusFS)
    "LocalRaft",
    "LockState",
    "LockInfo",
    "HolderInfo",
    # Helper
    "require_local_raft",
]
