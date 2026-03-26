"""Nexus Service Factory — userspace init system for NexusFS.

.. important:: ARCHITECTURAL DECISION (Task #23)

    This module is **NOT kernel code**. It lives at ``nexus/factory/``
    (top-level, alongside ``server/``, ``cli/``, ``services/``) by design.

    **Linux analogy**: NexusFS kernel = ``/kernel/``. This factory = ``systemd``
    (``/usr/lib/systemd/``). Systemd knows which services to start and how to
    wire them together, but it is not part of the kernel.

    **Why it exists**: The NexusFS kernel (``nexus.core.nexus_fs.NexusFS``)
    accepts pre-built services via dependency injection and never auto-creates
    them. This factory provides the default wiring so that callers don't have
    to manually construct 10 services every time.

Usage::

    # Quick: single call creates kernel + services
    from nexus.factory import create_nexus_fs

    nx = create_nexus_fs(
        backend=CASLocalBackend(root_path="./data"),
        metadata_store=RaftMetadataStore.embedded("./raft"),
        record_store=SQLAlchemyRecordStore(db_path="./db.sqlite"),
        permissions=PermissionConfig(enforce=False),
    )

    # Advanced: create services separately, inject into kernel
    from nexus.factory import create_nexus_services

    services = create_nexus_services(
        record_store=record_store,
        metadata_store=metadata_store,
        backend=backend,
        router=my_router,
    )
    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        services=services,
    )
"""

# Public API
from nexus.factory._background import _start_background_services

# Re-exports for backward compatibility (Issue #2180)
from nexus.factory._boot_context import _BootContext
from nexus.factory._bricks import _boot_dependent_bricks
from nexus.factory._bricks import _boot_independent_bricks as _boot_brick_services
from nexus.factory._helpers import (
    _make_gate,
    _safe_create,
)
from nexus.factory._kernel import _boot_kernel_services
from nexus.factory._metadata_export import create_metadata_export_service
from nexus.factory._record_store import create_record_store
from nexus.factory._system import _boot_pre_kernel_services
from nexus.factory._wired import _boot_post_kernel_services
from nexus.factory.adapters import _NexusFSFileReader
from nexus.factory.orchestrator import create_nexus_fs, create_nexus_services
from nexus.factory.wallet import WalletProvisioner

# Backward compatibility aliases (must follow imports)
_boot_services = _boot_pre_kernel_services
_boot_system_services = _boot_pre_kernel_services
_boot_core_services = _boot_pre_kernel_services
_boot_wired_services = _boot_post_kernel_services

__all__ = [
    "create_nexus_fs",
    "create_nexus_services",
    "create_record_store",
    "create_metadata_export_service",
]
