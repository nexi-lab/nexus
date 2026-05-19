"""Raft consensus for Nexus.

This module provides Python bindings to Rust Raft nodes for metadata
and lock operations.

Two access modes:
1. KernelClient (gRPC) - Kernel access via the nexus-cluster process
2. ZoneHandle (Rust-internal) - Multi-zone Raft consensus (~2-10ms)

REMOTE profile uses RPCTransport → NexusVFSService (see nexus.remote).
Federation lives entirely in the Rust kernel now — the cluster
profile binary (`nexusd-cluster`) owns the orchestrator; Python
callers reach it through KernelClient gRPC or the federation_* RPCs
in `nexus.server.rpc.services.federation_rpc`.

Architecture:
    Embedded:   NexusFS -> KernelClient (gRPC) -> nexus-cluster -> redb (~5μs + RPC)
    Consensus:  NexusFS -> KernelClient (gRPC) -> nexus-cluster -> Raft -> redb (~2-10ms)
    Remote:     NexusFS -> RPCTransport -> NexusVFSService (gRPC) -> server (~50-100ms)

Example (Metastore - embedded mode):
    from nexus.raft import Metastore

    store = Metastore("/var/lib/nexus/metadata")
    store.set_metadata("/path/to/file", metadata_bytes)
    metadata = store.get_metadata("/path/to/file")

Example (consensus mode — via syscalls + federation control-plane):
    from nexus.remote.kernel_client import KernelClient
    kernel = KernelClient(...)
    kernel.open()
    # Mount-tied lifecycle: sys_setattr DT_MOUNT auto-creates zones.
    kernel.sys_setattr("/data", entry_type=2, backend_name="federation",
                       zone_id="shared-zone")
    # Read/write through kernel.sys_* like any path; mount routes to the
    # shared-zone raft group.  Read federation state via the
    # ``/__sys__/zones/`` procfs view.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# =========================================================================
# Metastore (managed by nexus-cluster process)
# =========================================================================
_HAS_METASTORE = False
Metastore: Any = None
LockState: Any = None
LockInfo: Any = None
HolderInfo: Any = None

# PyO3 FFI classes no longer exist — the kernel is fully Rust and
# accessed via gRPC (KernelClient). These symbols are kept as None
# for any remaining type-check or isinstance guards in downstream code.

# =========================================================================
# ZoneHandle: Per-zone Raft node handle — now internal to the Rust kernel
# =========================================================================
ZoneHandle: Any = None

# Multi-zone federation: orchestration moved into the cluster binary
# (rust/cluster/) and the federation_* RPCs in
# nexus.server.rpc.services.federation_rpc, which delegate to PyKernel
# `zone_*` methods. There is no Python ZoneManager class anymore —
# Python callers either go through the RPC service or call kernel
# methods directly.


def require_metastore() -> None:
    """Require Metastore (sled driver) to be available.

    Call this before using Metastore to get a clear error message.

    Raises:
        RuntimeError: If Metastore is not available
    """
    if not _HAS_METASTORE:
        raise RuntimeError(
            "Metastore is not available. Build with:\n"
            "  cargo build --release -p nexus-cluster\n"
            "Or install the pre-built wheel:\n"
            "  pip install nexus-ai-fs"
        )


__all__ = [
    # PyO3 FFI: Metastore driver (embedded mode)
    "Metastore",
    # Per-zone Raft node handle (now internal to the Rust kernel; kept as
    # None for type-check compatibility)
    "ZoneHandle",
    # Lock types
    "LockState",
    "LockInfo",
    "HolderInfo",
    # Helper
    "require_metastore",
]
