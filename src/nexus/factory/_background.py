"""Start background threads after all tiers are constructed."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _start_background_services(system: dict[str, Any]) -> None:
    """Start background threads after all tiers are constructed.

    Deferred from tier construction so that all services are wired before
    any background I/O begins.

    Issue #2193: deferred_permission_buffer and write_observer moved from
    kernel dict to system dict (they are now system-tier services).

    Args:
        system: Services dict from ``_boot_system_services()``.
    """
    # Deferred Permission Buffer (former kernel tier, now system tier)
    dpb = system.get("deferred_permission_buffer")
    if dpb is not None and hasattr(dpb, "start"):
        dpb.start()
        logger.debug("[BOOT:BG] DeferredPermissionBuffer started")

    # Write Observer — PipedRecordStoreWriteObserver.start() is async,
    # called from server lifespan after PipeManager injection (Issue #809).
    # RecordStoreWriteObserver (SQLite fallback) has no start().

    # Event Delivery Worker (system tier)
    # Issue #3193: start() is now async — auto-started by
    # ServiceRegistry.start_background_services() (Q3).

    # Zone Lifecycle — load Terminating zones from DB (Issue #2061)
    zl = system.get("zone_lifecycle")
    if zl is not None and hasattr(zl, "load_terminating_zones"):
        try:
            session_factory = getattr(zl, "_session_factory", None)
            if session_factory is not None:
                with session_factory() as session:
                    zl.load_terminating_zones(session)
                logger.debug("[BOOT:BG] ZoneLifecycleService loaded terminating zones")
        except Exception as exc:
            logger.warning("[BOOT:BG] Failed to load terminating zones: %s", exc)
