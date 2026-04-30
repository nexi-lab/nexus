"""Setup / shutdown hooks for the activity subsystem.

Both functions are synchronous so they can be registered on
``FunctionPairComponent`` from ``nexus.server.lifespan.observability``.
The asyncio tasks they spawn require a running event loop — which is
present because the registry calls them inside ``async def start()``.
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


def setup_activity() -> None:
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
            "activity SQLiteSink failed to open at %s — falling back to NoopSink",
            cfg.db_path,
            exc_info=True,
        )
        sinks.append(NoopSink())

    worker = ActivityWorker(
        queue=queue,
        sinks=sinks,
        batch_size=cfg.batch_size,
        batch_timeout_s=cfg.batch_timeout_s,
    )
    retention = RetentionTask(db_path=cfg.db_path, retention_days=cfg.retention_days)

    loop = asyncio.get_running_loop()
    loop.create_task(worker.start())
    loop.create_task(retention.start())

    set_emitter(QueueEmitter(queue=queue))

    _STATE["worker"] = worker
    _STATE["retention"] = retention
    _STATE["queue"] = queue


def shutdown_activity() -> None:
    """Stop worker + retention. Safe to call when setup_activity skipped."""
    set_emitter(NoopEmitter())  # stop accepting new events first
    worker = _STATE.pop("worker", None)
    retention = _STATE.pop("retention", None)
    _STATE.pop("queue", None)
    loop = asyncio.get_running_loop()
    if isinstance(worker, ActivityWorker):
        loop.create_task(worker.stop(timeout=5.0))
    if isinstance(retention, RetentionTask):
        loop.create_task(retention.stop())
