"""Factory for EventLogProtocol implementations.

Tries the Rust WAL backend.  Returns None if unavailable (graceful degrade).

PGEventLog was removed in Issue #1241 — event delivery is now handled by
the transactional outbox pattern (``EventDeliveryWorker``).

Tracked by: #1397, #1241
"""

import logging

from nexus.system_services.event_subsystem.log.protocol import EventLogConfig, EventLogProtocol

logger = logging.getLogger(__name__)


def create_event_log(
    config: EventLogConfig,
) -> EventLogProtocol | None:
    """Create the best available EventLogProtocol implementation.

    Args:
        config: WAL / event log configuration.

    Returns:
        An EventLogProtocol instance, or None if no backend is available.
        Graceful degradation: callers must handle None (skip event logging).

    Note:
        PGEventLog was removed in Issue #1241.  Event delivery from the
        ``operation_log`` table is now handled by ``EventDeliveryWorker``
        (transactional outbox pattern with at-least-once semantics).
    """
    # Prefer Rust WAL
    try:
        from nexus.system_services.event_subsystem.log.wal import WALEventLog, is_available

        if is_available():
            log: EventLogProtocol = WALEventLog(config)
            logger.info("Event log: Rust WAL backend")
            return log
    except Exception as exc:
        logger.warning("Rust WAL unavailable: %s", exc)

    logger.info(
        "Event log: disabled (Rust WAL unavailable; "
        "event delivery via transactional outbox — Issue #1241)"
    )
    return None
