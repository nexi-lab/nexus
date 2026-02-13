"""Factory for EventLogProtocol implementations.

Tries the Rust WAL first; falls back to PostgreSQL if the extension
is not installed.  Returns None if neither is available (graceful degrade).

Tracked by: #1397
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.services.event_log.protocol import EventLogConfig, EventLogProtocol

logger = logging.getLogger(__name__)


def create_event_log(
    config: EventLogConfig,
    session_factory: Any | None = None,
) -> EventLogProtocol | None:
    """Create the best available EventLogProtocol implementation.

    Args:
        config: WAL / event log configuration.
        session_factory: SQLAlchemy sync session factory (required for PG fallback).

    Returns:
        An EventLogProtocol instance, or None if no backend is available.
        Graceful degradation: callers must handle None (skip event logging).
    """
    # Prefer Rust WAL
    try:
        from nexus.services.event_log.wal_backend import WALEventLog, is_available

        if is_available():
            log: EventLogProtocol = WALEventLog(config)
            logger.info("Event log: Rust WAL backend")
            return log
    except Exception as exc:
        logger.warning("Rust WAL unavailable: %s", exc)

    # Fallback to PostgreSQL
    if session_factory is not None:
        try:
            from nexus.services.event_log.pg_backend import PGEventLog

            pg_log: EventLogProtocol = PGEventLog(config, session_factory)
            logger.info("Event log: PostgreSQL fallback backend")
            return pg_log
        except Exception as exc:
            logger.warning("PostgreSQL event log unavailable: %s", exc)

    logger.info("Event log: disabled (no backend available, graceful degrade)")
    return None
