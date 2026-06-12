"""PostgreSQL timeout helpers for Tiger write-through paths."""

from __future__ import annotations

import logging
import math
import os
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

_DEFAULT_TIGER_WRITE_TIMEOUT_SECONDS = 5.0
_TIGER_WRITE_TIMEOUT_ENV = "NEXUS_TIGER_WRITE_TIMEOUT_SECONDS"


def _tiger_write_timeout_ms() -> int:
    raw = os.getenv(_TIGER_WRITE_TIMEOUT_ENV)
    if raw is None or raw.strip() == "":
        seconds = _DEFAULT_TIGER_WRITE_TIMEOUT_SECONDS
    else:
        try:
            seconds = float(raw)
        except ValueError:
            logger.warning(
                "Invalid %s=%r; using default %.1fs",
                _TIGER_WRITE_TIMEOUT_ENV,
                raw,
                _DEFAULT_TIGER_WRITE_TIMEOUT_SECONDS,
            )
            seconds = _DEFAULT_TIGER_WRITE_TIMEOUT_SECONDS
        else:
            if not math.isfinite(seconds) or seconds <= 0:
                logger.warning(
                    "Invalid %s=%r; using default %.1fs",
                    _TIGER_WRITE_TIMEOUT_ENV,
                    raw,
                    _DEFAULT_TIGER_WRITE_TIMEOUT_SECONDS,
                )
                seconds = _DEFAULT_TIGER_WRITE_TIMEOUT_SECONDS

    return max(1, int(seconds * 1000))


def apply_tiger_write_timeouts(conn: "Connection") -> None:
    """Bound lock and statement waits for a Tiger write-through transaction."""

    timeout_ms = _tiger_write_timeout_ms()
    conn.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    conn.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}ms'"))
