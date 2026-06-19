"""Start background threads after all tiers are constructed."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _start_background_services(system: dict[str, Any]) -> None:
    """Start background threads after all tiers are constructed.

    Deferred from tier construction so that all services are wired before
    any background I/O begins.

    DeferredPermissionBuffer and EventDeliveryWorker implement
    BackgroundService and are auto-started by the Rust kernel's
    service_start_all() at bootstrap(). No manual start needed here.

    Args:
        system: Services dict from ``_boot_system_services()``.
    """
    # Write Observer — RecordStoreWriteObserver (OBSERVE-phase) has no
    # start(). It is registered via hook_spec at factory enlist time.
    # The sync RecordStoreWriteObserver (SQLite fallback) also has no start().
