"""Raft consensus client for Nexus.

This module provides Python clients to communicate with Rust Raft nodes
for metadata and lock operations.

Three access modes:
1. Metastore (PyO3 FFI) - Direct redb access for embedded mode (~5μs)
2. ZoneManager + ZoneHandle (PyO3 FFI) - Multi-zone Raft consensus (~2-10ms)
3. RaftClient (gRPC) - For RemoteNexusFS to access Raft cluster (~200μs)

Architecture:
    Embedded:   NexusFS -> Metastore (PyO3) -> redb (~5μs)
    Consensus:  NexusFS -> ZoneManager -> ZoneHandle (PyO3) -> Raft -> redb (~2-10ms)
    Remote:     RemoteNexusFS -> RaftClient (gRPC) -> Raft cluster (~200μs)

Example (Metastore - embedded mode):
    from nexus.raft import Metastore

    store = Metastore("/var/lib/nexus/metadata")
    store.set_metadata("/path/to/file", metadata_bytes)
    metadata = store.get_metadata("/path/to/file")

Example (ZoneManager - consensus mode):
    from nexus.raft import ZoneManager

    mgr = ZoneManager(1, "/var/lib/nexus/zones", "0.0.0.0:2126")
    handle = mgr.create_zone("default", ["2@peer:2126"])
    handle.set_metadata("/path/to/file", metadata_bytes)  # replicated via consensus

Example (RaftClient - remote):
    from nexus.raft import RaftClient

    async with RaftClient("10.0.0.2:2026") as client:
        await client.put_metadata(file_metadata)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# =========================================================================
# gRPC client for remote Raft access (used by RemoteNexusFS)
# Declare with Any so mypy doesn't complain about None fallback.
# =========================================================================
_HAS_GRPC_CLIENT = False
RemoteLockInfo: Any = None
LockResult: Any = None
RaftClient: Any = None
RaftClientConfig: Any = None
RaftClientPool: Any = None
RaftError: Any = None
RaftNotLeaderError: Any = None

try:
    from nexus.raft import client as _raft_client_mod

    RemoteLockInfo = _raft_client_mod.LockInfo  # Renamed to avoid conflict with PyO3 LockInfo
    LockResult = _raft_client_mod.LockResult
    RaftClient = _raft_client_mod.RaftClient
    RaftClientConfig = _raft_client_mod.RaftClientConfig
    RaftClientPool = _raft_client_mod.RaftClientPool
    RaftError = _raft_client_mod.RaftError
    RaftNotLeaderError = _raft_client_mod.RaftNotLeaderError
    _HAS_GRPC_CLIENT = True
except ImportError:
    logger.debug(
        "RaftClient not available (protobuf code not generated). "
        "This is expected in CI/testing environments."
    )

# =========================================================================
# PyO3 FFI: Metastore (direct redb access, built by maturin)
# =========================================================================
_HAS_METASTORE = False
Metastore: Any = None
LockState: Any = None
LockInfo: Any = None
HolderInfo: Any = None

try:
    import _nexus_raft as _pyo3_mod

    Metastore = _pyo3_mod.Metastore
    LockState = _pyo3_mod.LockState
    LockInfo = _pyo3_mod.LockInfo
    HolderInfo = _pyo3_mod.HolderInfo
    _HAS_METASTORE = True
except ImportError:
    logger.debug(
        "Metastore not available. Install with: "
        "maturin develop -m rust/nexus_raft/Cargo.toml --features python"
    )

# =========================================================================
# ZoneHandle: Per-zone Raft node handle (requires --features full)
# =========================================================================
ZoneHandle: Any = None
try:
    import _nexus_raft as _pyo3_mod2

    ZoneHandle = _pyo3_mod2.ZoneHandle
except (ImportError, AttributeError):
    pass

# Python wrappers for multi-zone federation
from nexus.raft.zone_aware_metadata import ZoneAwareMetadataStore
from nexus.raft.zone_manager import ZoneManager
from nexus.raft.zone_path_resolver import ZonePathResolver


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
    # PyO3 FFI: Metastore driver (embedded mode)
    "Metastore",
    # Multi-zone federation
    "ZoneAwareMetadataStore",
    "ZoneManager",
    "ZonePathResolver",
    "ZoneHandle",
    # Lock types
    "LockState",
    "LockInfo",
    "HolderInfo",
    # Helper
    "require_metastore",
]
