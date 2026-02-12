"""Factory for EventLogProtocol implementations.

Tries the Rust WAL first; falls back to PostgreSQL if the extension
is not installed.  The factory is the single entry point used by
server startup and tests.

Tracked by: #1397
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.core.protocols.event_log import EventLogConfig, EventLogProtocol

logger = logging.getLogger(__name__)


def create_event_log(
    config: EventLogConfig,
    session_factory: Any | None = None,
) -> EventLogProtocol:
    """Create the best available EventLogProtocol implementation.

    Args:
        config: WAL / event log configuration.
        session_factory: SQLAlchemy sync session factory (required for PG fallback).

    Returns:
        An EventLogProtocol instance (WALEventLog or PGEventLog).

    Raises:
        RuntimeError: If neither backend is available.
    """
    # Prefer Rust WAL
    try:
        from nexus.core.event_log_wal import WALEventLog, is_available

        if is_available():
            log: EventLogProtocol = WALEventLog(config)
            logger.info("Event log: Rust WAL backend")
            return log
    except Exception as exc:
        logger.warning("Rust WAL unavailable: %s", exc)

    # Fallback to PostgreSQL
    if session_factory is not None:
        from nexus.core.event_log_pg import PGEventLog

        pg_log: EventLogProtocol = PGEventLog(config, session_factory)
        logger.info("Event log: PostgreSQL fallback backend")
        return pg_log

    raise RuntimeError(
        "No event log backend available. "
        "Install _nexus_wal (maturin develop) or provide a session_factory for PG."
    )
