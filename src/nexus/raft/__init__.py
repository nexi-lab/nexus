"""Raft consensus client for Nexus.

This module provides Python clients to communicate with Rust Raft nodes
for metadata and lock operations.

Two metastore drivers are provided:
1. Metastore (PyO3 FFI) - Direct sled access for embedded/EC mode (~5μs)
2. RaftConsensus (PyO3 FFI) - Full Raft consensus for SC mode (replicated writes)

Plus a gRPC client for remote access:
3. RaftClient (gRPC) - For RemoteNexusFS to access Raft cluster (remote mode)

Architecture:
    Embedded:   NexusFS -> Metastore (PyO3) -> sled (~5μs)
    SC mode:    NexusFS -> RaftConsensus (PyO3) -> Raft consensus -> sled (~2-10ms)
    EC mode:    NexusFS -> RaftConsensus (PyO3, lazy=True) -> local apply + bg propose (~5μs)
    Remote:     RemoteNexusFS -> RaftClient (gRPC) -> Raft cluster (~200μs)

Example (Metastore - embedded mode):
    from nexus.raft import Metastore

    store = Metastore("/var/lib/nexus/metadata")
    store.set_metadata("/path/to/file", metadata_bytes)
    metadata = store.get_metadata("/path/to/file")

Example (RaftConsensus - SC mode):
    from nexus.raft import RaftConsensus

    node = RaftConsensus(1, "/var/lib/nexus/metadata", "0.0.0.0:2126", ["2@peer:2126"])
    node.set_metadata("/path/to/file", metadata_bytes)  # replicated via consensus

Example (RaftClient - remote):
    from nexus.raft import RaftClient

    async with RaftClient("10.0.0.2:2026") as client:
        await client.put_metadata(file_metadata)
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# gRPC client for remote Raft access (used by RemoteNexusFS)
# Requires generated protobuf code (metadata_pb2, transport_pb2, etc.)
try:
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

    _HAS_GRPC_CLIENT = True
except ImportError:
    # Generated protobuf code not available (CI, pure-Python installs)
    _HAS_GRPC_CLIENT = False
    RemoteLockInfo = None  # type: ignore[assignment,misc]
    LockResult = None  # type: ignore[assignment,misc]
    RaftClient = None  # type: ignore[assignment,misc]
    RaftClientConfig = None  # type: ignore[assignment,misc]
    RaftClientPool = None  # type: ignore[assignment,misc]
    RaftError = None  # type: ignore[assignment,misc]
    RaftNotLeaderError = None  # type: ignore[assignment,misc]
    logger.debug(
        "RaftClient not available (protobuf code not generated). "
        "This is expected in CI/testing environments."
    )

# PyO3 FFI: Metastore (direct sled access, built by maturin)
# Import from _nexus_raft (top-level, like nexus_fast)
if TYPE_CHECKING:
    from _nexus_raft import (
        HolderInfo as HolderInfo,
    )
    from _nexus_raft import (
        LockInfo as LockInfo,
    )
    from _nexus_raft import (
        LockState as LockState,
    )
    from _nexus_raft import (
        Metastore as Metastore,
    )

try:
    from _nexus_raft import (
        HolderInfo,
        LockInfo,
        LockState,
        Metastore,
    )

    _HAS_METASTORE = True
except ImportError:
    # Native module not available - maturin build required
    # Run: maturin develop -m rust/nexus_raft/Cargo.toml --features python
    _HAS_METASTORE = False
    Metastore = None
    LockState = None
    LockInfo = None
    HolderInfo = None
    logger.debug(
        "Metastore not available. Install with: "
        "maturin develop -m rust/nexus_raft/Cargo.toml --features python"
    )

# RaftConsensus: Full Raft consensus for SC mode (requires --features full)
RaftConsensus = None
with contextlib.suppress(ImportError):
    from _nexus_raft import RaftConsensus


def require_metastore() -> None:
    """Require Metastore (sled driver) to be available.

    Call this before using Metastore to get a clear error message.

    Raises:
        RuntimeError: If Metastore is not available
    """
    if not _HAS_METASTORE:
        raise RuntimeError(
            "Metastore is not available. Build with:\n"
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
    # PyO3 FFI: Metastore driver (embedded/EC mode)
    "Metastore",
    # PyO3 FFI: Raft consensus driver (SC mode)
    "RaftConsensus",
    # Lock types
    "LockState",
    "LockInfo",
    "HolderInfo",
    # Helper
    "require_metastore",
]
