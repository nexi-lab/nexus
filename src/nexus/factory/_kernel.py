"""Boot Tier 0 (KERNEL) — validate Storage Pillars are functional.

Per NEXUS-LEGO-ARCHITECTURE §2 and Liedtke's microkernel test, only VFS
routing and Metastore belong in the kernel.  Both are injected as
constructor arguments (kernel, metadata_store) and validated here.

All service creation has been moved to ``_boot_system_services()``
(Issue #2193) where services are classified as *critical* (BootError)
or *degradable* (WARNING + None).
"""

import logging
import time
from typing import Any

from nexus.factory._boot_context import _BootContext

logger = logging.getLogger(__name__)


def _boot_kernel_services(ctx: _BootContext) -> dict[str, Any]:
    """Boot Tier 0 (KERNEL) — validate Storage Pillars are functional.

    Checks that the VFS router and metadata store are present and
    operational.  Raises ``BootError`` if validation fails.

    Issue #2193: All 11 former-kernel services (ReBAC, permissions,
    workspace, write-sync) moved to ``_boot_system_services()`` Tier 1.

    Returns:
        Empty dict — kernel services are validated, not created.
    """
    from nexus.contracts.exceptions import BootError

    t0 = time.perf_counter()
    try:
        if ctx.kernel is None:
            raise BootError("VFS kernel is None", tier="kernel")

        if ctx.metadata_store is None:
            raise BootError("Metadata store is None", tier="kernel")

        if ctx.record_store is None:
            logger.warning("[BOOT:KERNEL] RecordStore is None — services layer disabled")

        elapsed = time.perf_counter() - t0
        logger.info("[BOOT:KERNEL] Storage pillars validated (%.3fs)", elapsed)
        return {}

    except BootError:
        raise
    except Exception as exc:
        logger.critical("[BOOT:KERNEL] Fatal: %s", exc)
        raise BootError(str(exc), tier="kernel") from exc
