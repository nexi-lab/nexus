"""Temporal parsing utilities for Memory API (Issue #1023).

Provides ISO-8601 date parsing with support for partial dates
(year, year-month) for temporal query operators.

Based on SimpleMem research (arXiv:2601.02553) which achieves
58.62 F1 on temporal reasoning vs 48.91 for Mem0.
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

    Args:
        value: String datetime, datetime object, or None

    Returns:
        datetime object in UTC, or None

    Raises:
        ValueError: If string cannot be parsed as a valid datetime

    Examples:
        >>> parse_datetime("2025-01-08T14:30:00Z")
        datetime(2025, 1, 8, 14, 30, 0, tzinfo=timezone.utc)

        >>> parse_datetime("2025-01-08")
        datetime(2025, 1, 8, 0, 0, 0, tzinfo=timezone.utc)

        >>> parse_datetime("2025-01")
        datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        >>> parse_datetime("2025")
        datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        # Ensure timezone aware
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    value = value.strip()

    # Try full ISO-8601 with timezone
    try:
        # Handle 'Z' suffix
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
    """Parse a partial date string into a start/end datetime range.

    Supports:
    - Year: "2025" → (2025-01-01T00:00:00Z, 2025-12-31T23:59:59Z)
    - Year-month: "2025-01" → (2025-01-01T00:00:00Z, 2025-01-31T23:59:59Z)
    - Date: "2025-01-08" → (2025-01-08T00:00:00Z, 2025-01-08T23:59:59Z)
    - Full datetime: "2025-01-08T14:30:00Z" → (exact time, exact time)

    Args:
        value: Partial or full date string

    Returns:
        TemporalRange with start and end datetimes

    Raises:
        ValueError: If string cannot be parsed

    Examples:
        >>> parse_temporal_range("2025")
        TemporalRange(
            start=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            end=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
        )

        >>> parse_temporal_range("2025-01")
        TemporalRange(
            start=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            end=datetime(2025, 1, 31, 23, 59, 59, tzinfo=UTC)
        )
    """
    value = value.strip()

    # Year only: YYYY
    if re.match(r"^\d{4}$", value):
        year = int(value)
        start = datetime(year, 1, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC)
        return TemporalRange(start, end)

    # Year-month: YYYY-MM
    if re.match(r"^\d{4}-\d{2}$", value):
        start = datetime.strptime(value + "-01", "%Y-%m-%d").replace(tzinfo=UTC)
        # Calculate last day of month
        if start.month == 12:
            end = datetime(start.year + 1, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
        else:
            end = datetime(start.year, start.month + 1, 1, tzinfo=UTC) - timedelta(
                seconds=1
            )
        return TemporalRange(start, end)

    # Date only: YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        start = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
        end = start.replace(hour=23, minute=59, second=59)
        return TemporalRange(start, end)

    # Full datetime - return same time for both
    dt = parse_datetime(value)
    if dt is None:
        raise ValueError(f"Cannot parse temporal range: {value!r}")
    return TemporalRange(dt, dt)


def validate_temporal_params(
    after: str | datetime | None = None,
    before: str | datetime | None = None,
    during: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Validate and normalize temporal query parameters.

    Handles mutual exclusivity and converts all parameters to
    datetime objects for database queries.

    Args:
        after: Return memories created after this time
        before: Return memories created before this time
        during: Return memories created during this period (partial date)

    Returns:
        Tuple of (after_dt, before_dt) for database filtering

    Raises:
        ValueError: If parameters are invalid or mutually exclusive

    Examples:
        >>> validate_temporal_params(after="2025-01-01")
        (datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC), None)

        >>> validate_temporal_params(during="2025-01")
        (datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
         datetime(2025, 1, 31, 23, 59, 59, tzinfo=UTC))

        >>> validate_temporal_params(after="2025-01-01", during="2025-01")
        ValueError: Cannot use 'during' with 'after' or 'before'
    """
    # Check mutual exclusivity
    if during and (after or before):
        raise ValueError("Cannot use 'during' with 'after' or 'before'")

    # Handle 'during' parameter
    if during:
        temporal_range = parse_temporal_range(during)
        return temporal_range.start, temporal_range.end

    # Handle 'after' and 'before' parameters
    after_dt = parse_datetime(after)
    before_dt = parse_datetime(before)

    # Validate range makes sense
    if after_dt and before_dt and after_dt > before_dt:
        raise ValueError(
            f"'after' ({after_dt.isoformat()}) must be before 'before' ({before_dt.isoformat()})"
        )

    return after_dt, before_dt
