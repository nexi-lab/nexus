"""Setup / shutdown hooks for the activity subsystem.

Both functions are async so ``FunctionPairComponent`` (the registry-side
adapter) can await them. Awaiting matters at shutdown: the worker has
queued events that must drain to the SQLite sink before the registry
declares the component stopped.
"""

from __future__ import annotations

import asyncio
import logging

from nexus.services.activity.config import ActivityConfig
from nexus.services.activity.emitter import (
    NoopEmitter,
    QueueEmitter,
    set_emitter,
)
from nexus.services.activity.events import ActivityEvent
from nexus.services.activity.retention import RetentionTask
from nexus.services.activity.sinks import NoopSink, SQLiteSink
from nexus.services.activity.sinks.protocol import SinkProtocol
from nexus.services.activity.worker import ActivityWorker

logger = logging.getLogger(__name__)

_STATE: dict[str, object] = {"worker": None, "retention": None, "queue": None}


async def setup_activity() -> None:
    """Start activity worker + retention task. Safe to call once per process."""
    cfg = ActivityConfig.from_env()
    if not cfg.enabled:
        set_emitter(NoopEmitter())
        logger.info("activity subsystem disabled by NEXUS_ACTIVITY_ENABLED=0")
        return

    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=cfg.queue_size)

    sinks: list[SinkProtocol] = []
    try:
        sinks.append(SQLiteSink(path=cfg.db_path))
        logger.info("activity SQLite sink open at %s", cfg.db_path)
    except Exception:
        logger.error(
            "activity SQLiteSink failed to open at %s — falling back to NoopSink. "
            "Durable activity_events store is DISABLED for this process.",
            cfg.db_path,
            exc_info=True,
        )
        # Surface the degradation in /metrics so operators have an alertable
        # signal — without this, ACTIVITY_SINK_ERRORS stays at 0 while every
        # event is silently discarded.
        try:
            from nexus.services.activity.metrics import ACTIVITY_SINK_ERRORS

            ACTIVITY_SINK_ERRORS.labels(sink="SQLiteSink").inc()
        except Exception:
            pass
        sinks.append(NoopSink())

    worker = ActivityWorker(
        queue=queue,
        sinks=sinks,
        batch_size=cfg.batch_size,
        batch_timeout_s=cfg.batch_timeout_s,
    )
    retention = RetentionTask(db_path=cfg.db_path, retention_days=cfg.retention_days)

    loop = asyncio.get_running_loop()
    await worker.start()
    await retention.start()

    queue_emitter = QueueEmitter(queue=queue, loop=loop)
    set_emitter(queue_emitter)

    _STATE["worker"] = worker
    _STATE["retention"] = retention
    _STATE["queue"] = queue
    _STATE["emitter"] = queue_emitter


async def shutdown_activity() -> None:
    """Stop worker + retention. Safe to call when setup_activity skipped.

    Order matters:
    1. Install NoopEmitter so new emit() calls become no-ops.
    2. Quiesce the previous QueueEmitter — off-loop emits schedule
       call_soon_threadsafe callbacks that have not yet enqueued, and
       must run before the worker drain closes the queue.
    3. Stop retention BEFORE the worker so retention's VACUUM cannot
       hold the SQLite write lock during the final drain.
    4. Stop the worker and close sinks.
    """
    prev_emitter = _STATE.pop("emitter", None)
    set_emitter(NoopEmitter())  # stop accepting new events first
    worker = _STATE.pop("worker", None)
    retention = _STATE.pop("retention", None)
    _STATE.pop("queue", None)
    if isinstance(prev_emitter, QueueEmitter):
        try:
            await prev_emitter.quiesce_pending(timeout=2.0)
        except Exception:
            logger.warning("activity emitter quiesce failed", exc_info=True)
    if isinstance(retention, RetentionTask):
        try:
            await retention.stop()
        except Exception:
            logger.warning("activity retention stop failed", exc_info=True)
    if isinstance(worker, ActivityWorker):
        try:
            await worker.stop(timeout=10.0)
        except Exception:
            logger.warning("activity worker stop failed", exc_info=True)
