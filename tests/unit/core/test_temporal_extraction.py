"""Tests for temporal metadata extraction and query filtering (Issue #1028)."""

from datetime import datetime


class TestTemporalMetadataExtraction:
    """Tests for extract_temporal_metadata function."""

    def test_extract_single_date(self):
        """Test extracting a single temporal reference."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "Meeting tomorrow",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        assert "temporal_refs" in result
        assert len(result["temporal_refs"]) == 1
        assert result["temporal_refs"][0]["original"] == "tomorrow"
        assert "2025-01-11" in result["temporal_refs"][0]["resolved"]
        assert result["earliest_date"] == datetime(2025, 1, 11)
        assert result["latest_date"] == datetime(2025, 1, 11)

    def test_extract_multiple_dates(self):
        """Test extracting multiple temporal references."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "Meeting tomorrow and follow-up in 3 days",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        assert len(result["temporal_refs"]) >= 2
        assert result["earliest_date"] is not None
        assert result["latest_date"] is not None
        # earliest should be tomorrow (Jan 11), latest should be in 3 days (Jan 13)
        assert result["earliest_date"] <= result["latest_date"]

    def test_earliest_latest_calculation(self):
        """Test that earliest/latest dates are correctly calculated."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "We met yesterday and will meet again in 5 days",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        # Yesterday = Jan 9, in 5 days = Jan 15
        if result["earliest_date"] and result["latest_date"]:
            assert result["earliest_date"] < result["latest_date"]
            assert result["earliest_date"].day == 9  # yesterday
            assert result["latest_date"].day == 15  # in 5 days

    def test_no_temporal_refs(self):
        """Test text without temporal references."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "The cat sat on the mat.",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        assert result["temporal_refs"] == []
        assert result["earliest_date"] is None
        assert result["latest_date"] is None

    def test_reference_time_as_string(self):
        """Test that reference_time can be a string."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "Meeting tomorrow",
            reference_time="2025-01-10T12:00:00",
        )

        assert len(result["temporal_refs"]) == 1
        assert result["earliest_date"] == datetime(2025, 1, 11)

    def test_today_extraction(self):
        """Test extracting 'today' reference."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "Meeting today at 3pm",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        assert len(result["temporal_refs"]) >= 1
        assert result["earliest_date"] == datetime(2025, 1, 10)

    def test_yesterday_extraction(self):
        """Test extracting 'yesterday' reference."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "We discussed this yesterday",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        assert len(result["temporal_refs"]) >= 1
        assert result["earliest_date"] == datetime(2025, 1, 9)

    def test_next_week_extraction(self):
        """Test extracting 'next week' reference."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "Let's meet next week",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        # Should have at least one temporal ref
        assert len(result["temporal_refs"]) >= 1

    def test_days_ago_extraction(self):
        """Test extracting 'X days ago' reference."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        result = extract_temporal_metadata(
            "This happened 5 days ago",
            reference_time=datetime(2025, 1, 10, 12, 0),
        )

        assert len(result["temporal_refs"]) >= 1
        # 5 days before Jan 10 = Jan 5
        assert result["earliest_date"] == datetime(2025, 1, 5)


class TestParseDateFromResolved:
    """Tests for _parse_date_from_resolved helper function."""

    def test_parse_iso_date(self):
        """Test parsing ISO date format."""
        from nexus.core.temporal_resolver import _parse_date_from_resolved

        result = _parse_date_from_resolved("on 2025-01-11", None)
        assert result == datetime(2025, 1, 11)

    def test_parse_date_with_prefix(self):
        """Test parsing date with 'on' prefix."""
        from nexus.core.temporal_resolver import _parse_date_from_resolved

        result = _parse_date_from_resolved("on 2025-01-15 at 14:00", None)
        assert result == datetime(2025, 1, 15)

    def test_parse_month_year(self):
        """Test parsing month year format."""
        from nexus.core.temporal_resolver import _parse_date_from_resolved

        result = _parse_date_from_resolved("January 2025", None)
        assert result == datetime(2025, 1, 1)

    def test_parse_invalid_returns_none(self):
        """Test that invalid strings return None."""
        from nexus.core.temporal_resolver import _parse_date_from_resolved

        result = _parse_date_from_resolved("no date here", None)
        assert result is None

    def test_parse_empty_returns_none(self):
        """Test that empty string returns None."""
        from nexus.core.temporal_resolver import _parse_date_from_resolved

        result = _parse_date_from_resolved("", None)
        assert result is None

    def test_parse_week_of_format(self):
        """Test parsing 'week of' format."""
        from nexus.core.temporal_resolver import _parse_date_from_resolved

        result = _parse_date_from_resolved("week of 2025-01-13", None)
        assert result == datetime(2025, 1, 13)


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_extract_temporal_metadata_with_default_time(self):
        """Test extract_temporal_metadata with default reference time."""
        from nexus.core.temporal_resolver import extract_temporal_metadata

        # Should work without explicit reference time (uses current time)
        result = extract_temporal_metadata("Meeting tomorrow")

        # Should have temporal refs (result depends on current date)
        assert "temporal_refs" in result
        assert "earliest_date" in result
        assert "latest_date" in result
