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
    startup_tasks: set[asyncio.Task[None]] = set()
    t1 = loop.create_task(worker.start())
    t2 = loop.create_task(retention.start())
    startup_tasks.add(t1)
    startup_tasks.add(t2)
    t1.add_done_callback(startup_tasks.discard)
    t2.add_done_callback(startup_tasks.discard)
    _STATE["startup_tasks"] = startup_tasks

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
    shutdown_tasks: set[asyncio.Task[None]] = set()
    if isinstance(worker, ActivityWorker) or isinstance(retention, RetentionTask):
        loop = asyncio.get_running_loop()
        if isinstance(worker, ActivityWorker):
            t = loop.create_task(worker.stop(timeout=5.0))
            shutdown_tasks.add(t)
            t.add_done_callback(shutdown_tasks.discard)
        if isinstance(retention, RetentionTask):
            t = loop.create_task(retention.stop())
            shutdown_tasks.add(t)
            t.add_done_callback(shutdown_tasks.discard)
    _STATE["shutdown_tasks"] = shutdown_tasks
