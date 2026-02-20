"""Temporal parsing utilities for Memory brick.

Brick-local copy of nexus.core.temporal (Issue #2177).
Provides ISO-8601 date parsing with support for partial dates
(year, year-month) for temporal query operators.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import NamedTuple


class TemporalRange(NamedTuple):
    """A temporal range with start and end datetimes."""

    start: datetime
    end: datetime


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse a datetime from string or pass through datetime object.

    Supports:
    - ISO-8601 full datetime: "2025-01-08T14:30:00Z"
    - ISO-8601 with offset: "2025-01-08T14:30:00+00:00"
    - Date only: "2025-01-08" (assumes start of day UTC)
    - Year-month: "2025-01" (assumes start of month UTC)
    - Year only: "2025" (assumes start of year UTC)
    - datetime object (passed through)
    - None (returns None)
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    value = value.strip()

    # Try full ISO-8601 with timezone
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass

    # Try date only: YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)

    # Try year-month: YYYY-MM
    if re.match(r"^\d{4}-\d{2}$", value):
        return datetime.strptime(value + "-01", "%Y-%m-%d").replace(tzinfo=UTC)

    # Try year only: YYYY
    if re.match(r"^\d{4}$", value):
        return datetime.strptime(value + "-01-01", "%Y-%m-%d").replace(tzinfo=UTC)

    raise ValueError(f"Cannot parse datetime: {value!r}")


def parse_temporal_range(value: str) -> TemporalRange:
    """Parse a partial date string into a start/end datetime range."""
    value = value.strip()

    if re.match(r"^\d{4}$", value):
        year = int(value)
        start = datetime(year, 1, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC)
        return TemporalRange(start, end)

    if re.match(r"^\d{4}-\d{2}$", value):
        start = datetime.strptime(value + "-01", "%Y-%m-%d").replace(tzinfo=UTC)
        if start.month == 12:
            end = datetime(start.year + 1, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
        else:
            end = datetime(start.year, start.month + 1, 1, tzinfo=UTC) - timedelta(seconds=1)
        return TemporalRange(start, end)

    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        start = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
        end = start.replace(hour=23, minute=59, second=59)
        return TemporalRange(start, end)

    dt = parse_datetime(value)
    if dt is None:
        raise ValueError(f"Cannot parse temporal range: {value!r}")
    return TemporalRange(dt, dt)


def validate_temporal_params(
    after: str | datetime | None = None,
    before: str | datetime | None = None,
    during: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Validate and normalize temporal query parameters."""
    if during and (after or before):
        raise ValueError("Cannot use 'during' with 'after' or 'before'")

    if during:
        temporal_range = parse_temporal_range(during)
        return temporal_range.start, temporal_range.end

    after_dt = parse_datetime(after)
    before_dt = parse_datetime(before)

    if after_dt and before_dt and after_dt > before_dt:
        raise ValueError(
            f"'after' ({after_dt.isoformat()}) must be before 'before' ({before_dt.isoformat()})"
        )

    return after_dt, before_dt
