"""Raft consensus for Nexus.

Since R20.18.6, federation is owned entirely by the Rust kernel (see
`Kernel::init_federation_from_env` + `/__sys__/zones/` procfs). This
module exposes only the embedded-mode Metastore PyO3 class + lock
data types; `ZoneManager` / `ZoneHandle` pyclasses were deleted. Their
names are kept here as `None` so legacy imports still resolve until
every caller migrates off.

REMOTE profile uses RPCTransport → NexusVFSService (see nexus.remote).

Architecture:
    Embedded:   NexusFS -> Metastore (PyO3) -> redb (~5μs)
    Consensus:  NexusFS -> PyKernel.zone_* / federation_rpc -> Rust -> raft (~2-10ms)
    Remote:     NexusFS -> RPCTransport -> NexusVFSService (gRPC) -> server (~50-100ms)
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

# R20.18.6: PyZoneManager + PyZoneHandle pyclasses deleted. Names are
# kept here as `None` so legacy imports keep resolving; any remaining
# caller that tries to use them will blow up loudly at call time.
ZoneHandle: Any = None
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
