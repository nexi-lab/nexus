"""Start background threads after all tiers are constructed."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _start_background_services(kernel: dict[str, Any], system: dict[str, Any]) -> None:
    """Start background threads after all tiers are constructed.

    Deferred from tier construction so that all services are wired before
    any background I/O begins.
    """
    # Deferred Permission Buffer (kernel tier)
    dpb = kernel.get("deferred_permission_buffer")
    if dpb is not None and hasattr(dpb, "start"):
        dpb.start()
        logger.debug("[BOOT:BG] DeferredPermissionBuffer started")

    # Write Observer — only BufferedRecordStoreWriteObserver needs .start()
    wo = kernel.get("write_observer")
    if wo is not None and hasattr(wo, "start"):
        from nexus.storage.record_store_syncer import BufferedRecordStoreWriteObserver

        if isinstance(wo, BufferedRecordStoreWriteObserver):
            wo.start()
            logger.debug("[BOOT:BG] BufferedRecordStoreWriteObserver started")

    # Event Delivery Worker (system tier)
    dw = system.get("delivery_worker")
    if dw is not None and hasattr(dw, "start"):
        dw.start()
        logger.debug("[BOOT:BG] EventDeliveryWorker started")
