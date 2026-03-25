"""CAS Garbage Collector — background task for physical content cleanup.

Periodically scans CAS .meta sidecars for ref_count=0 entries past a grace
period, then deletes the corresponding blob + meta files.

Each CASAddressingEngine instance owns its own GC — no shared state, no
federation concerns (each node GCs its own local transport).

Design:
    - Grace period: uses ``released_at`` timestamp in meta sidecar
    - Scan interval is configurable (default 60s)
    - GC runs as an asyncio.Task, started/stopped by the engine owner
    - Thread-safe: blob deletion is idempotent (already-deleted = no-op)

Issue #1320: CAS async GC.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.base.cas_addressing_engine import CASAddressingEngine

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_GRACE_PERIOD_S = 300.0  # 5 minutes
DEFAULT_SCAN_INTERVAL_S = 60.0  # 1 minute


class CASGarbageCollector:
    """Background GC for CAS blobs with ref_count=0.

    Usage::

        gc = CASGarbageCollector(engine)
        gc.start()   # spawns asyncio.Task
        ...
        await gc.stop()  # cancels task, waits for clean exit
    """

    def __init__(
        self,
        engine: CASAddressingEngine,
        *,
        grace_period: float = DEFAULT_GRACE_PERIOD_S,
        scan_interval: float = DEFAULT_SCAN_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._grace_period = grace_period
        self._scan_interval = scan_interval
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def start(self) -> None:
        """Start GC background task in the current event loop."""
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.ensure_future(self._run())
        logger.info(
            "CAS GC started for %s (grace=%ds, interval=%ds)",
            self._engine.name,
            self._grace_period,
            self._scan_interval,
        )

    async def stop(self) -> None:
        """Stop GC background task."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("CAS GC stopped for %s", self._engine.name)

    async def _run(self) -> None:
        """Main GC loop — scan + collect on interval."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._scan_interval)
                if self._stopped:
                    break
                self._collect()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("CAS GC scan error for %s", self._engine.name, exc_info=True)

    def _collect(self) -> None:
        """Single GC pass — find and delete ref_count=0 blobs past grace period.

        Grace period is checked via ``released_at`` timestamp in the meta
        sidecar. If ``released_at`` is missing (legacy), the blob is treated
        as immediately eligible.
        """
        engine = self._engine
        transport = engine._transport
        now = time.time()
        collected = 0

        # list_blobs returns (blob_keys, common_prefixes)
        try:
            blob_keys, _ = transport.list_blobs(prefix="cas/", delimiter="")
        except Exception:
            logger.debug("CAS GC: list_blobs failed for %s", engine.name, exc_info=True)
            return

        meta_keys = [k for k in blob_keys if k.endswith(".meta")]

        for meta_key in meta_keys:
            try:
                meta_data, _ = transport.get_blob(meta_key)
                meta = json.loads(meta_data)

                if meta.get("ref_count", 1) > 0:
                    continue

                # Check grace period via released_at timestamp in meta
                released_at = meta.get("released_at", 0.0)
                if released_at > 0 and (now - released_at) < self._grace_period:
                    continue

                # ref_count=0 and past grace period — delete blob + meta
                blob_key = meta_key[: -len(".meta")]
                with contextlib.suppress(Exception):
                    transport.delete_blob(blob_key)
                with contextlib.suppress(Exception):
                    transport.delete_blob(meta_key)

                # Evict from meta cache
                content_hash = blob_key.split("/")[-1]
                if engine._meta_cache is not None:
                    engine._meta_cache.pop(content_hash, None)

                collected += 1
            except Exception:
                continue  # Skip broken entries, continue scanning

        if collected > 0:
            logger.info("CAS GC: collected %d blobs for %s", collected, engine.name)
