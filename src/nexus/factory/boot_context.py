"""Boot context dataclass shared across tier boot functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _BootContext:
    """Shared dependencies passed between tier boot functions.

    Built once at the start of ``create_nexus_services()`` and threaded
    through ``_boot_kernel_services``, ``_boot_system_services``, and
    ``_boot_brick_services`` so each tier function receives a clean,
    immutable snapshot of the boot-time configuration.
    """

    record_store: Any
    metadata_store: Any
    backend: Any
    router: Any
    engine: Any
    read_engine: Any  # Read replica engine (Issue #725); same as engine when no replica
    session_factory: Any
    perm: Any  # PermissionConfig
    cache_ttl_seconds: int | None
    dist: Any  # DistributedConfig
    zone_id: str | None
    agent_id: str | None
    enable_write_buffer: bool | None
    resiliency_raw: dict[str, Any] | None
    db_url: str
    profile_tuning: Any  # ProfileTuning (Issue #2071)
