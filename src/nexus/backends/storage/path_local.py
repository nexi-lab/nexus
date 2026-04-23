"""Path-based local filesystem backend (no CAS overhead).

Thin subclass of PathAddressingEngine that:
- Creates a LocalTransport for raw local I/O
- Registers as "path_local" via @register_connector
- Exposes root_path / has_root_path for orchestrator

Storage structure:
    root_path/
    └── workspace/
        └── file.txt          # Stored at actual path

Naming convention: {addressing}_{transport} per Section 5.2 of
docs/architecture/backend-architecture.md.

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.transports.local_transport import LocalTransport
from nexus.contracts.backend_features import BLOB_BACKEND_FEATURES, BackendFeature


@register_connector("path_local")
class PathLocalBackend(PathAddressingEngine):
    """Local filesystem backend with direct path mapping.

    Unlike ``CASLocalBackend`` (CAS addressing + local transport), this uses
    path addressing + local transport.  Files are stored at their actual
    virtual path — no hashing, no dedup, no Bloom filters.

    Use cases:
    - Minimal/dev profiles where CAS overhead isn't needed
    - When files should be at predictable disk locations
    - Testing with simpler storage

    Opt into CAS by setting ``backend = "local"`` in config.
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = BLOB_BACKEND_FEATURES | frozenset(
        {
            BackendFeature.ROOT_PATH,
        }
    )

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "root_path": ConnectionArg(
            type=ArgType.PATH,
            description="Root directory for storage",
            required=True,
            config_key="data_dir",
        ),
    }

    def __init__(self, root_path: str | Path, *, fsync: bool = True) -> None:
        self.root_path = Path(root_path).resolve()
        transport = LocalTransport(root_path=self.root_path, fsync=fsync)
        super().__init__(transport, backend_name="path_local")

    @property
    def has_root_path(self) -> bool:
        return True
