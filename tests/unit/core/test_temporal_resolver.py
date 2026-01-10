"""Tests for temporal expression resolution (Issue #1027)."""

from datetime import datetime

import pytest

from nexus.core.temporal_resolver import (
    HeuristicTemporalResolver,
    LLMTemporalResolver,
    TemporalResult,
    get_temporal_resolver,
    resolve_temporal,
)


class TestTemporalResult:
    """Tests for TemporalResult dataclass."""

    def test_temporal_result_basic(self):
        """Test basic TemporalResult creation."""
        ref_time = datetime(2025, 1, 10, 12, 0)
        result = TemporalResult(
            resolved_text="Meeting on 2025-01-11 at 14:00",
            original_text="Meeting tomorrow at 2pm",
            replacements=[
                {"original": "tomorrow", "resolved": "on 2025-01-11", "type": "tomorrow"}
            ],
            reference_time=ref_time,
            method="heuristic",
        )
        assert result.resolved_text == "Meeting on 2025-01-11 at 14:00"
        assert result.original_text == "Meeting tomorrow at 2pm"
        assert result.method == "heuristic"
        assert result.reference_time == ref_time
        assert len(result.replacements) == 1

    def test_temporal_result_defaults(self):
        """Test TemporalResult with default values."""
        result = TemporalResult(
            resolved_text="Test",
            original_text="Test",
        )
        assert result.replacements == []
        assert result.method == "none"
        assert result.reference_time is None


class TestHeuristicTemporalResolver:
    """Tests for the heuristic-based temporal resolver."""

    @pytest.fixture
    def resolver(self):
        """Create HeuristicTemporalResolver instance."""
        return HeuristicTemporalResolver()

    @pytest.fixture
    def ref_time(self):
        """Reference time: Friday, January 10, 2025, 12:00 PM."""
        return datetime(2025, 1, 10, 12, 0)

    def test_no_temporal_refs(self, resolver, ref_time):
        """Test text without temporal references returns unchanged."""
        text = "John went to the store."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == text
        assert result.method == "none"
        assert len(result.replacements) == 0

    def test_today_resolution(self, resolver, ref_time):
        """Test 'today' replacement."""
        text = "The meeting is today."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == "The meeting is on 2025-01-10."
        assert result.method == "heuristic"
        assert len(result.replacements) == 1
        assert result.replacements[0]["type"] == "today"

    def test_tomorrow_resolution(self, resolver, ref_time):
        """Test 'tomorrow' replacement."""
        text = "I'll call you tomorrow."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == "I'll call you on 2025-01-11."
        assert result.method == "heuristic"
        assert len(result.replacements) == 1
        assert result.replacements[0]["type"] == "tomorrow"

    def test_yesterday_resolution(self, resolver, ref_time):
        """Test 'yesterday' replacement."""
        text = "I saw her yesterday."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == "I saw her on 2025-01-09."
        assert result.method == "heuristic"
        assert len(result.replacements) == 1
        assert result.replacements[0]["type"] == "yesterday"

    def test_in_n_days_resolution(self, resolver, ref_time):
        """Test 'in N days' replacement."""
        text = "The deadline is in 3 days."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == "The deadline is on 2025-01-13."
        assert result.method == "heuristic"
        assert result.replacements[0]["type"] == "in_n_days"

    def test_in_1_day_resolution(self, resolver, ref_time):
        """Test 'in 1 day' replacement (singular)."""
        text = "I'll finish it in 1 day."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == "I'll finish it on 2025-01-11."

    def test_n_days_ago_resolution(self, resolver, ref_time):
        """Test 'N days ago' replacement."""
        text = "It happened 5 days ago."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == "It happened on 2025-01-05."
        assert result.method == "heuristic"
        assert result.replacements[0]["type"] == "n_days_ago"

    def test_next_monday_resolution(self, resolver, ref_time):
        """Test 'next Monday' replacement (ref is Friday Jan 10)."""
        text = "We'll meet next Monday."
        result = resolver.resolve(text, ref_time)
        # Next Monday from Friday Jan 10 is Jan 13
        assert result.resolved_text == "We'll meet on 2025-01-13."
        assert result.method == "heuristic"
        assert result.replacements[0]["type"] == "next_weekday"

    def test_next_friday_resolution(self, resolver, ref_time):
        """Test 'next Friday' replacement (ref is Friday Jan 10)."""
        text = "See you next Friday."
        result = resolver.resolve(text, ref_time)
        # Next Friday from Friday Jan 10 is Jan 17
        assert result.resolved_text == "See you on 2025-01-17."

    def test_last_monday_resolution(self, resolver, ref_time):
        """Test 'last Monday' replacement (ref is Friday Jan 10)."""
        text = "We met last Monday."
        result = resolver.resolve(text, ref_time)
        # Last Monday from Friday Jan 10 is Jan 6
        assert result.resolved_text == "We met on 2025-01-06."
        assert result.replacements[0]["type"] == "last_weekday"

    def test_last_friday_resolution(self, resolver, ref_time):
        """Test 'last Friday' replacement (ref is Friday Jan 10)."""
        text = "It was last Friday."
        result = resolver.resolve(text, ref_time)
        # Last Friday from Friday Jan 10 is Jan 3
        assert result.resolved_text == "It was on 2025-01-03."

    def test_next_week_resolution(self, resolver, ref_time):
        """Test 'next week' replacement."""
        text = "Let's schedule it for next week."
        result = resolver.resolve(text, ref_time)
        # Next Monday from Friday Jan 10 is Jan 13
        assert "the week of 2025-01-13" in result.resolved_text
        assert result.replacements[0]["type"] == "next_week"

    def test_last_week_resolution(self, resolver, ref_time):
        """Test 'last week' replacement."""
        text = "It happened last week."
        result = resolver.resolve(text, ref_time)
        # Last Monday from Friday Jan 10 is Dec 30, 2024
        assert "the week of 2024-12-30" in result.resolved_text
        assert result.replacements[0]["type"] == "last_week"

    def test_next_month_resolution(self, resolver, ref_time):
        """Test 'next month' replacement."""
        text = "Payment is due next month."
        result = resolver.resolve(text, ref_time)
        assert "February 2025" in result.resolved_text
        assert result.replacements[0]["type"] == "next_month"

    def test_last_month_resolution(self, resolver, ref_time):
        """Test 'last month' replacement."""
        text = "We launched last month."
        result = resolver.resolve(text, ref_time)
        assert "December 2024" in result.resolved_text
        assert result.replacements[0]["type"] == "last_month"

    def test_multiple_temporal_refs(self, resolver, ref_time):
        """Test multiple temporal references in one text."""
        text = "We met yesterday and will meet again tomorrow."
        result = resolver.resolve(text, ref_time)
        assert "on 2025-01-09" in result.resolved_text
        assert "on 2025-01-11" in result.resolved_text
        assert len(result.replacements) == 2

    def test_preserves_non_temporal_text(self, resolver, ref_time):
        """Test that non-temporal text is preserved."""
        text = "John said he'll call tomorrow about the project."
        result = resolver.resolve(text, ref_time)
        assert "John said he'll call" in result.resolved_text
        assert "about the project" in result.resolved_text
        assert "on 2025-01-11" in result.resolved_text

    def test_case_insensitive(self, resolver, ref_time):
        """Test case-insensitive matching."""
        text = "Meeting TOMORROW and also Today."
        result = resolver.resolve(text, ref_time)
        assert "on 2025-01-11" in result.resolved_text
        assert "on 2025-01-10" in result.resolved_text

    def test_default_reference_time(self, resolver):
        """Test that reference_time defaults to now."""
        text = "Meeting tomorrow."
        result = resolver.resolve(text)  # No reference time
        assert result.reference_time is not None
        assert "on" in result.resolved_text

    def test_year_boundary_next_month(self, resolver):
        """Test next month at year boundary (December -> January)."""
        ref_time = datetime(2024, 12, 15, 12, 0)
        text = "Payment due next month."
        result = resolver.resolve(text, ref_time)
        assert "January 2025" in result.resolved_text

    def test_year_boundary_last_month(self, resolver):
        """Test last month at year boundary (January -> December)."""
        ref_time = datetime(2025, 1, 15, 12, 0)
        text = "We launched last month."
        result = resolver.resolve(text, ref_time)
        assert "December 2024" in result.resolved_text


class TestLLMTemporalResolver:
    """Tests for the LLM-based temporal resolver."""

    @pytest.fixture
    def resolver(self):
        """Create LLMTemporalResolver without LLM provider."""
        return LLMTemporalResolver(llm_provider=None)

    @pytest.fixture
    def ref_time(self):
        """Reference time: Friday, January 10, 2025, 12:00 PM."""
        return datetime(2025, 1, 10, 12, 0)

    def test_no_temporal_refs_skips_llm(self, resolver, ref_time):
        """Test that text without temporal refs skips LLM call."""
        text = "John went to the store."
        result = resolver.resolve(text, ref_time)
        assert result.resolved_text == text
        assert result.method == "none"

    def test_has_temporal_expressions_detection(self, resolver):
        """Test temporal expression detection."""
        assert resolver._has_temporal_expressions("Meeting tomorrow.")
        assert resolver._has_temporal_expressions("Call me yesterday.")
        assert resolver._has_temporal_expressions("In 3 days.")
        assert resolver._has_temporal_expressions("5 days ago.")
        assert resolver._has_temporal_expressions("Next Monday.")
        assert resolver._has_temporal_expressions("Last week.")
        assert resolver._has_temporal_expressions("This weekend.")
        assert not resolver._has_temporal_expressions("John went to store.")
        assert not resolver._has_temporal_expressions("The date is 2025-01-10.")

    def test_fallback_to_heuristic(self, resolver, ref_time):
        """Test fallback to heuristic when no LLM available."""
        text = "Meeting tomorrow."
        result = resolver.resolve(text, ref_time)
        # Should fall back to heuristic since no LLM provider
        assert result.method == "heuristic"
        assert "on 2025-01-11" in result.resolved_text

    def test_extract_resolved_text_markers(self, resolver):
        """Test extraction of resolved text with various markers."""
        original = "Meeting tomorrow."

        # Test "Resolved:" marker
        response = "Reasoning: tomorrow is Jan 11.\nResolved: Meeting on 2025-01-11."
        result = resolver._extract_resolved_text(response, original)
        assert result == "Meeting on 2025-01-11."

        # Test quoted text
        response = '"Meeting on 2025-01-11."'
        result = resolver._extract_resolved_text(response, original)
        assert result == "Meeting on 2025-01-11."

    def test_extract_resolved_text_malformed(self, resolver):
        """Test that malformed responses return original."""
        original = "Meeting tomorrow about the important project."

        # Response too short
        response = "Hi"
        result = resolver._extract_resolved_text(response, original)
        assert result == original

        # Response too long
        response = original * 5
        result = resolver._extract_resolved_text(response, original)
        assert result == original

    def test_detect_replacements(self, resolver):
        """Test replacement detection."""
        original = "Meeting tomorrow and yesterday."
        resolved = "Meeting on 2025-01-11 and on 2025-01-09."
        replacements = resolver._detect_replacements(original, resolved)
        assert len(replacements) >= 2


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_get_temporal_resolver_default(self):
        """Test getting default resolver."""
        resolver = get_temporal_resolver()
        assert isinstance(resolver, LLMTemporalResolver)

    def test_get_temporal_resolver_with_provider(self):
        """Test getting resolver with custom provider."""
        mock_provider = object()
        resolver = get_temporal_resolver(llm_provider=mock_provider)
        assert isinstance(resolver, LLMTemporalResolver)
        assert resolver.llm_provider is mock_provider

    def test_resolve_temporal_function(self):
        """Test the convenience resolve_temporal function."""
        ref_time = datetime(2025, 1, 10, 12, 0)
        result = resolve_temporal(
            text="Meeting tomorrow.",
            reference_time=ref_time,
        )
        assert "on 2025-01-11" in result

    def test_resolve_temporal_no_temporal_refs(self):
        """Test resolve_temporal with no temporal expressions."""
        result = resolve_temporal(text="John went to the store.")
        assert result == "John went to the store."

    def test_resolve_temporal_string_reference_time(self):
        """Test resolve_temporal with ISO string reference time."""
        result = resolve_temporal(
            text="Meeting tomorrow.",
            reference_time="2025-01-10T12:00:00",
        )
        assert "on 2025-01-11" in result

    def test_resolve_temporal_iso_with_z(self):
        """Test resolve_temporal with ISO string ending in Z."""
        result = resolve_temporal(
            text="Meeting tomorrow.",
            reference_time="2025-01-10T12:00:00Z",
        )
        assert "on 2025-01-11" in result
