"""Nexus Service Factory — userspace init system for NexusFS.

.. important:: ARCHITECTURAL DECISION (Task #23)

    This module is **NOT kernel code**. It lives at ``nexus/factory/``
    (top-level, alongside ``server/``, ``cli/``, ``services/``) by design.

    **Linux analogy**: NexusFS kernel = ``/kernel/``. This factory = ``systemd``
    (``/usr/lib/systemd/``). Systemd knows which services to start and how to
    wire them together, but it is not part of the kernel.

Usage::

    # Quick: single call creates kernel + services
    from nexus.factory import create_nexus_fs

    nx = create_nexus_fs(
        backend=LocalBackend(root_path="./data"),
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
    nx = NexusFS(backend=backend, metadata_store=metadata_store, services=services)
"""

from nexus.factory.adapters import _NexusFSFileReader
from nexus.factory.boot_context import _BootContext
from nexus.factory.bricks import _boot_brick_services
from nexus.factory.compose import create_nexus_fs, create_nexus_services, create_record_store
from nexus.factory.kernel import _boot_kernel_services
from nexus.factory.system import _boot_system_services, _start_background_services

__all__ = [
    "_BootContext",
    "_NexusFSFileReader",
    "_boot_brick_services",
    "_boot_kernel_services",
    "_boot_system_services",
    "_start_background_services",
    "create_nexus_fs",
    "create_nexus_services",
    "create_record_store",
]
