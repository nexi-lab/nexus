"""Unit tests for temporal parsing utilities (Issue #1023)."""

from datetime import UTC, datetime

import pytest

from nexus.core.temporal import (
    TemporalRange,
    parse_datetime,
    parse_temporal_range,
    validate_temporal_params,
)


class TestParseDatetime:
    """Tests for parse_datetime function."""

    def test_none_input(self) -> None:
        """Test None input returns None."""
        assert parse_datetime(None) is None

    def test_datetime_passthrough(self) -> None:
        """Test datetime object passes through."""
        dt = datetime(2025, 1, 8, 14, 30, 0, tzinfo=UTC)
        result = parse_datetime(dt)
        assert result == dt

    def test_datetime_adds_timezone_if_missing(self) -> None:
        """Test that naive datetime gets UTC timezone added."""
        dt = datetime(2025, 1, 8, 14, 30, 0)
        result = parse_datetime(dt)
        assert result.tzinfo == UTC

    def test_full_iso_with_z(self) -> None:
        """Test full ISO-8601 with Z suffix."""
        result = parse_datetime("2025-01-08T14:30:00Z")
        assert result == datetime(2025, 1, 8, 14, 30, 0, tzinfo=UTC)

    def test_full_iso_with_offset(self) -> None:
        """Test full ISO-8601 with offset."""
        result = parse_datetime("2025-01-08T14:30:00+00:00")
        assert result == datetime(2025, 1, 8, 14, 30, 0, tzinfo=UTC)

    def test_date_only(self) -> None:
        """Test date-only string (YYYY-MM-DD)."""
        result = parse_datetime("2025-01-08")
        assert result == datetime(2025, 1, 8, 0, 0, 0, tzinfo=UTC)

    def test_year_month(self) -> None:
        """Test year-month string (YYYY-MM)."""
        result = parse_datetime("2025-01")
        assert result == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_year_only(self) -> None:
        """Test year-only string (YYYY)."""
        result = parse_datetime("2025")
        assert result == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_invalid_string_raises(self) -> None:
        """Test invalid string raises ValueError."""
        with pytest.raises(ValueError, match="Cannot parse datetime"):
            parse_datetime("not-a-date")

    def test_whitespace_stripped(self) -> None:
        """Test whitespace is stripped from input."""
        result = parse_datetime("  2025-01-08  ")
        assert result == datetime(2025, 1, 8, 0, 0, 0, tzinfo=UTC)


class TestParseTemporalRange:
    """Tests for parse_temporal_range function."""

    def test_year_range(self) -> None:
        """Test year string returns full year range."""
        result = parse_temporal_range("2025")
        assert result == TemporalRange(
            start=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            end=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        )

    def test_year_month_range_january(self) -> None:
        """Test year-month range for January."""
        result = parse_temporal_range("2025-01")
        assert result.start == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert result.end == datetime(2025, 1, 31, 23, 59, 59, tzinfo=UTC)

    def test_year_month_range_february(self) -> None:
        """Test year-month range for February (non-leap year)."""
        result = parse_temporal_range("2025-02")
        assert result.start == datetime(2025, 2, 1, 0, 0, 0, tzinfo=UTC)
        assert result.end == datetime(2025, 2, 28, 23, 59, 59, tzinfo=UTC)

    def test_year_month_range_february_leap(self) -> None:
        """Test year-month range for February (leap year)."""
        result = parse_temporal_range("2024-02")
        assert result.start == datetime(2024, 2, 1, 0, 0, 0, tzinfo=UTC)
        assert result.end == datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC)

    def test_year_month_range_december(self) -> None:
        """Test year-month range for December (year boundary)."""
        result = parse_temporal_range("2025-12")
        assert result.start == datetime(2025, 12, 1, 0, 0, 0, tzinfo=UTC)
        assert result.end == datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)

    def test_date_range(self) -> None:
        """Test full date returns day range."""
        result = parse_temporal_range("2025-01-08")
        assert result.start == datetime(2025, 1, 8, 0, 0, 0, tzinfo=UTC)
        assert result.end == datetime(2025, 1, 8, 23, 59, 59, tzinfo=UTC)

    def test_full_datetime_range(self) -> None:
        """Test full datetime returns same time for both."""
        result = parse_temporal_range("2025-01-08T14:30:00Z")
        assert result.start == datetime(2025, 1, 8, 14, 30, 0, tzinfo=UTC)
        assert result.end == datetime(2025, 1, 8, 14, 30, 0, tzinfo=UTC)

    def test_invalid_string_raises(self) -> None:
        """Test invalid string raises ValueError."""
        with pytest.raises(ValueError):
            parse_temporal_range("invalid")


class TestValidateTemporalParams:
    """Tests for validate_temporal_params function."""

    def test_no_params_returns_none(self) -> None:
        """Test no parameters returns (None, None)."""
        after_dt, before_dt = validate_temporal_params()
        assert after_dt is None
        assert before_dt is None

    def test_after_only(self) -> None:
        """Test after parameter only."""
        after_dt, before_dt = validate_temporal_params(after="2025-01-01")
        assert after_dt == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert before_dt is None

    def test_before_only(self) -> None:
        """Test before parameter only."""
        after_dt, before_dt = validate_temporal_params(before="2025-12-31")
        assert after_dt is None
        assert before_dt == datetime(2025, 12, 31, 0, 0, 0, tzinfo=UTC)

    def test_after_and_before(self) -> None:
        """Test both after and before parameters."""
        after_dt, before_dt = validate_temporal_params(
            after="2025-01-01", before="2025-12-31"
        )
        assert after_dt == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert before_dt == datetime(2025, 12, 31, 0, 0, 0, tzinfo=UTC)

    def test_during_year(self) -> None:
        """Test during parameter with year."""
        after_dt, before_dt = validate_temporal_params(during="2025")
        assert after_dt == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert before_dt == datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)

    def test_during_month(self) -> None:
        """Test during parameter with year-month."""
        after_dt, before_dt = validate_temporal_params(during="2025-01")
        assert after_dt == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert before_dt == datetime(2025, 1, 31, 23, 59, 59, tzinfo=UTC)

    def test_during_with_after_raises(self) -> None:
        """Test during with after raises ValueError."""
        with pytest.raises(ValueError, match="Cannot use 'during' with"):
            validate_temporal_params(during="2025", after="2025-01-01")

    def test_during_with_before_raises(self) -> None:
        """Test during with before raises ValueError."""
        with pytest.raises(ValueError, match="Cannot use 'during' with"):
            validate_temporal_params(during="2025", before="2025-12-31")

    def test_after_greater_than_before_raises(self) -> None:
        """Test after > before raises ValueError."""
        with pytest.raises(ValueError, match="'after'.*must be before 'before'"):
            validate_temporal_params(after="2025-12-31", before="2025-01-01")

    def test_datetime_objects(self) -> None:
        """Test datetime objects are accepted."""
        after = datetime(2025, 1, 1, tzinfo=UTC)
        before = datetime(2025, 12, 31, tzinfo=UTC)
        after_dt, before_dt = validate_temporal_params(after=after, before=before)
        assert after_dt == after
        assert before_dt == before
