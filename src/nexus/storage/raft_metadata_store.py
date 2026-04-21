"""Compat shim — R20.18.5 deletion-dominant cutover.

The real ``RaftMetadataStore`` class (1047 lines) was deleted when
federation moved into the Rust kernel. Every in-tree caller only used
``RaftMetadataStore.embedded(path)`` to obtain a ``MetastoreABC``; that
classmethod always returned a ``RustMetastoreProxy`` backed by a fresh
``nexus_kernel.Kernel``. This shim preserves the import path + the
``.embedded`` classmethod for test/benchmark callers until R20.17
cleans them up.

Direct instantiation (``RaftMetadataStore(engine=...)``) is not
supported post-R20.18.5. Callers that relied on the old constructor
should drive zone CRUD through the Rust kernel's ``sys_*`` surface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class RaftMetadataStore:
    """Thin shim forwarding ``.embedded(path)`` to ``RustMetastoreProxy``."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "RaftMetadataStore() constructor was deleted in R20.18.5. "
            "Use `RaftMetadataStore.embedded(path)` which returns a "
            "`RustMetastoreProxy`, or drive zone metadata through "
            "`nexus_kernel.Kernel` syscalls directly."
        )

    @classmethod
    def embedded(cls, db_path: str, zone_id: str | None = None) -> Any:
        """Return a ``RustMetastoreProxy`` for the given redb path.

        Matches the pre-R20.18.5 classmethod contract: callers receive
        a ``MetastoreABC`` wrapping a fresh kernel with the redb path
        wired in, so both Python ``self.metadata.*`` + kernel
        ``sys_*`` calls hit the same store.
        """
        del zone_id  # reserved for future per-zone embedded mode
        from nexus.core.metastore import RustMetastoreProxy

        try:
            from nexus_kernel import Kernel as _Kernel
        except ImportError as exc:
            raise RuntimeError(
                "nexus_kernel not available. Build with: maturin develop -m rust/kernel/Cargo.toml"
            ) from exc

        redb_path = db_path + (".redb" if not db_path.endswith(".redb") else "")
        # Ensure parent directory exists so the first write succeeds.
        Path(redb_path).parent.mkdir(parents=True, exist_ok=True)
        kernel = _Kernel()
        proxy = RustMetastoreProxy(kernel, redb_path)
        logger.info("Created embedded RustMetastoreProxy at %s", redb_path)
        return proxy
