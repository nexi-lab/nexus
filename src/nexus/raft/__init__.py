"""Raft consensus for Nexus.

This module provides Python bindings to Rust Raft nodes for metadata
and lock operations.

Two access modes:
1. Metastore (PyO3 FFI) - Direct redb access for embedded mode (~5μs)
2. ZoneManager + ZoneHandle (PyO3 FFI) - Multi-zone Raft consensus (~2-10ms)

REMOTE profile uses RPCTransport → NexusVFSService (see nexus.remote).
Federation is owned entirely by the Rust kernel since R20.18.5
(see `Kernel::init_federation_from_env` + `/__sys__/zones/` procfs).

Architecture:
    Embedded:   NexusFS -> Metastore (PyO3) -> redb (~5μs)
    Consensus:  NexusFS -> ZoneManager -> ZoneHandle (PyO3) -> Raft -> redb (~2-10ms)
    Remote:     NexusFS -> RPCTransport -> NexusVFSService (gRPC) -> server (~50-100ms)

Example (Metastore - embedded mode):
    from nexus.raft import Metastore

    store = Metastore("/var/lib/nexus/metadata")
    store.set_metadata("/path/to/file", metadata_bytes)
    metadata = store.get_metadata("/path/to/file")

Example (ZoneManager - consensus mode):
    from nexus.raft import ZoneManager

    mgr = ZoneManager(hostname="nexus-1", base_path="/var/lib/nexus/zones")  # bind_addr from NEXUS_BIND_ADDR env
    handle = mgr.create_zone("root", ["2@peer:2126"])
    handle.set_metadata("/path/to/file", metadata_bytes)  # replicated via consensus
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# =========================================================================
# PyO3 FFI: Metastore (direct redb access, built by maturin)
# =========================================================================
_HAS_METASTORE = False
Metastore: Any = None
LockState: Any = None
LockInfo: Any = None
HolderInfo: Any = None

try:
    # F2 C8 (Option A): raft's PyO3 classes were moved into the
    # ``nexus_kernel`` cdylib. A single .so holds Kernel + Metastore +
    # ZoneManager + ZoneHandle so raft's ``kernel::Metastore`` impls can
    # be installed as true Rust trait objects without cross-cdylib
    # duplication. Use ``getattr`` so mypy doesn't trip on stale stubs
    # while a locally-installed wheel lags behind.
    import nexus_kernel as _pyo3_mod

    Metastore = getattr(_pyo3_mod, "Metastore", None)
    LockState = getattr(_pyo3_mod, "LockState", None)
    LockInfo = getattr(_pyo3_mod, "LockInfo", None)
    HolderInfo = getattr(_pyo3_mod, "HolderInfo", None)
    _HAS_METASTORE = Metastore is not None
except ImportError:
    logger.debug("Metastore not available. Install with: maturin develop -m rust/kernel/Cargo.toml")

# =========================================================================
# ZoneHandle: Per-zone Raft node handle (requires --features full)
# =========================================================================
ZoneHandle: Any = None
try:
    import nexus_kernel as _pyo3_mod2

    ZoneHandle = getattr(_pyo3_mod2, "ZoneHandle", None)
except ImportError:
    pass

# R20.18.5: Python ZoneManager/RaftMetadataStore/federation shims were
# deleted — federation is now owned entirely by the Rust kernel (see
# `Kernel::init_federation_from_env` + `/__sys__/zones/` procfs). This
# symbol is kept as ``None`` so legacy imports keep resolving until
# their callers are retired.
ZoneManager: Any = None


def require_metastore() -> None:
    """Require Metastore (sled driver) to be available.

    Call this before using Metastore to get a clear error message.

    Raises:
        RuntimeError: If Metastore is not available
    """
    if not _HAS_METASTORE:
        raise RuntimeError(
            "Metastore is not available. Build with:\n"
            "  maturin develop -m rust/raft/Cargo.toml --features python\n"
            "Or install the pre-built wheel:\n"
            "  pip install nexus-ai-fs"
        )


__all__ = [
    # PyO3 FFI: Metastore driver (embedded mode)
    "Metastore",
    # Multi-zone federation
    "ZoneManager",
    "ZoneHandle",
    # Lock types
    "LockState",
    "LockInfo",
    "HolderInfo",
    # Helper
    "require_metastore",
]
