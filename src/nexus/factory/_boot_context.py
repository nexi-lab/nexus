"""Boot context dataclass — carries shared deps between tier functions."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend
    from nexus.contracts.types import AuditConfig
    from nexus.core.config import DistributedConfig, PermissionConfig
    from nexus.core.metastore import MetastoreABC
    from nexus.lib.performance_tuning import ProfileTuning
    from nexus.storage.record_store import RecordStoreABC


@dataclass(frozen=True)
class _BootContext:
    """Shared dependencies passed between boot functions.

    Built once at the start of ``create_nexus_services()`` and threaded
    through ``_boot_system_services`` and ``_boot_independent_bricks``
    so each boot function receives a clean, immutable snapshot of the
    boot-time configuration.
    """

    record_store: "RecordStoreABC"
    metadata_store: "MetastoreABC"
    backend: "Backend"
    kernel: Any
    dlc: Any
    engine: Any
    read_engine: Any  # Read replica engine (Issue #725); same as engine when no replica
    perm: "PermissionConfig"
    audit: "AuditConfig"
    cache_ttl_seconds: int | None
    dist: "DistributedConfig"
    zone_id: str | None
    agent_id: str | None
    enable_write_buffer: bool | None
    resiliency_raw: dict[str, Any] | None
    db_url: str
    profile_tuning: "ProfileTuning"

    # WAL config for EventLog (Issue #2195)
    wal_dir: str | None = None
    wal_sync_mode: str | None = None
    wal_segment_size: int | None = None

    # Issue #3193: shared signal for write-observer -> delivery-worker wakeup
    event_signal: asyncio.Event = field(default_factory=asyncio.Event)
