"""Tests for display_path utilities (Issue #3256).

Covers:
- sanitize_filename() edge cases (empty, special chars, long, reserved, Unicode)
- resolve_collisions() (unique, duplicates, empty, single item)
- DisplayPathMixin default behavior
- list_dir_metadata protocol (Issue #3266)
"""

from typing import Any

import pytest

from nexus.backends.connectors.cli.display_path import (
    DisplayPathMixin,
    resolve_collisions,
    sanitize_filename,
)

# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    """Parametrized edge case matrix for sanitize_filename()."""

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            # Normal strings
            ("Hello World", "Hello-World"),
            ("meeting-notes", "meeting-notes"),
            ("Re: Budget Review", "Re-Budget-Review"),
            # Special characters replaced with dash
            ('file<>:"/\\|?*name', "file-name"),
            ("path/with/slashes", "path-with-slashes"),
            # Collapse multiple separators
            ("too   many   spaces", "too-many-spaces"),
            ("dashes---everywhere", "dashes-everywhere"),
            ("mixed_-_separators", "mixed-separators"),
            # Leading/trailing dots and spaces
            ("...leading.dots", "leading.dots"),
            ("trailing.dots...", "trailing.dots"),
            (" spaces around ", "spaces-around"),
            # Unicode normalization (NFC)
            ("\u00e9", "\u00e9"),  # é stays as é (NFC)
            ("caf\u00e9", "caf\u00e9"),
            # Control characters
            ("hello\x00world", "hello-world"),
            ("tab\there", "tab-here"),
        ],
    )
    def test_basic_sanitization(self, input_name: str, expected: str) -> None:
        assert sanitize_filename(input_name) == expected

    @pytest.mark.parametrize(
        "input_name",
        [
            "",
            "   ",
            "\t\n",
            None,
        ],
    )
    def test_empty_or_whitespace_returns_fallback(self, input_name: str | None) -> None:
        result = sanitize_filename(input_name or "")
        assert result == "_unnamed"

    def test_all_special_chars_returns_fallback(self) -> None:
        result = sanitize_filename("???***///")
        assert result == "_unnamed"

    @pytest.mark.parametrize(
        "reserved",
        ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9", "con", "Con"],
    )
    def test_windows_reserved_names(self, reserved: str) -> None:
        result = sanitize_filename(reserved)
        assert result.startswith("_")
        assert reserved.upper() not in result.split(".")[0].upper() or result.startswith("_")

    def test_long_string_truncated(self) -> None:
        long_name = "A" * 300
        result = sanitize_filename(long_name)
        assert len(result) <= 140

    def test_truncated_string_has_hash_suffix(self) -> None:
        long_name = "A" * 300
        result = sanitize_filename(long_name)
        # Should end with _{6-char-hash}
        assert "_" in result
        parts = result.rsplit("_", 1)
        assert len(parts[1]) == 6

    def test_different_long_strings_produce_different_results(self) -> None:
        a = sanitize_filename("A" * 300)
        b = sanitize_filename("B" * 300)
        assert a != b

    def test_custom_max_len(self) -> None:
        result = sanitize_filename("A" * 50, max_len=20)
        assert len(result) <= 20

    def test_very_small_max_len(self) -> None:
        """max_len < 8 should not produce garbage or crash."""
        result = sanitize_filename("A" * 50, max_len=5)
        assert len(result) <= 5
        assert result  # non-empty

    def test_max_len_exactly_8(self) -> None:
        result = sanitize_filename("A" * 50, max_len=8)
        assert len(result) <= 8

    def test_short_string_unchanged_length(self) -> None:
        result = sanitize_filename("short")
        assert result == "short"

    def test_preserves_file_extension_in_name(self) -> None:
        result = sanitize_filename("report.pdf")
        assert result == "report.pdf"

    def test_email_subject_realistic(self) -> None:
        result = sanitize_filename("Re: Q4 Budget Review: 50% increase? Let's discuss!")
        assert "?" not in result
        assert ":" not in result
        assert len(result) > 10


# ---------------------------------------------------------------------------
# resolve_collisions
# ---------------------------------------------------------------------------


class TestResolveCollisions:
    def test_empty_list(self) -> None:
        assert resolve_collisions([]) == []

    def test_no_collisions(self) -> None:
        items = [
            ("INBOX/email1.yaml", "id-1"),
            ("INBOX/email2.yaml", "id-2"),
            ("INBOX/email3.yaml", "id-3"),
        ]
        result = resolve_collisions(items)
        assert result == items

    def test_collision_adds_hash_suffix(self) -> None:
        items = [
            ("INBOX/Meeting.yaml", "id-aaa"),
            ("INBOX/Meeting.yaml", "id-bbb"),
        ]
        result = resolve_collisions(items)
        # Both should have hash suffixes since they collide
        assert result[0][0] != result[1][0]
        assert result[0][0].startswith("INBOX/Meeting_")
        assert result[1][0].startswith("INBOX/Meeting_")
        assert result[0][0].endswith(".yaml")
        assert result[1][0].endswith(".yaml")
        # Backend IDs preserved
        assert result[0][1] == "id-aaa"
        assert result[1][1] == "id-bbb"

    def test_collision_suffix_is_deterministic(self) -> None:
        items = [
            ("INBOX/Meeting.yaml", "id-aaa"),
            ("INBOX/Meeting.yaml", "id-bbb"),
        ]
        result1 = resolve_collisions(items)
        result2 = resolve_collisions(items)
        assert result1 == result2

    def test_triple_collision(self) -> None:
        items = [
            ("path/file.yaml", "id-1"),
            ("path/file.yaml", "id-2"),
            ("path/file.yaml", "id-3"),
        ]
        result = resolve_collisions(items)
        paths = [r[0] for r in result]
        assert len(set(paths)) == 3  # All unique after resolution

    def test_different_directories_no_false_collision(self) -> None:
        items = [
            ("INBOX/Meeting.yaml", "id-1"),
            ("SENT/Meeting.yaml", "id-2"),
        ]
        result = resolve_collisions(items)
        # Different directories = no collision
        assert result == items

    def test_single_item_no_suffix(self) -> None:
        items = [("path/file.yaml", "id-1")]
        result = resolve_collisions(items)
        assert result == items

    def test_preserves_extension(self) -> None:
        items = [
            ("data.json", "id-1"),
            ("data.json", "id-2"),
        ]
        result = resolve_collisions(items)
        assert all(r[0].endswith(".json") for r in result)

    def test_mixed_collision_and_unique(self) -> None:
        items = [
            ("INBOX/unique.yaml", "id-1"),
            ("INBOX/dup.yaml", "id-2"),
            ("INBOX/dup.yaml", "id-3"),
            ("SENT/other.yaml", "id-4"),
        ]
        result = resolve_collisions(items)
        # unique and other should be unchanged
        assert result[0] == items[0]
        assert result[3] == items[3]
        # dups should be disambiguated
        assert result[1][0] != result[2][0]


# ---------------------------------------------------------------------------
# DisplayPathMixin
# ---------------------------------------------------------------------------


class TestDisplayPathMixin:
    def test_default_display_path(self) -> None:
        mixin = DisplayPathMixin()
        assert mixin.display_path("abc123") == "abc123.yaml"

    def test_default_with_metadata_ignored(self) -> None:
        mixin = DisplayPathMixin()
        result = mixin.display_path("abc123", {"subject": "Meeting"})
        assert result == "abc123.yaml"


# ---------------------------------------------------------------------------
# CLIConnector.list_dir_metadata default
# ---------------------------------------------------------------------------


class TestListDirMetadataDefault:
    """list_dir_metadata returns None by default (opt-in protocol)."""

    def test_base_returns_none(self) -> None:
        from nexus.backends.connectors.cli.base import CLIConnector

        # Use a minimal subclass so we can call the default method.
        class StubConnector(CLIConnector):
            pass

        connector = StubConnector.__new__(StubConnector)
        result = connector.list_dir_metadata("/some/path")
        assert result is None


# ---------------------------------------------------------------------------
# GmailConnector.list_dir_metadata
# ---------------------------------------------------------------------------


class TestGmailListDirMetadata:
    """GmailConnector.list_dir_metadata returns batch metadata."""

    @staticmethod
    def _make_gmail() -> Any:
        from nexus.backends.connectors.gws.connector import GmailConnector

        gmail = GmailConnector.__new__(GmailConnector)
        # Set up minimal state that list_dir_metadata needs.
        gmail._LABELS = ["INBOX", "SENT", "STARRED", "IMPORTANT", "DRAFTS"]
        return gmail

    def test_returns_none_for_root(self) -> None:
        gmail = self._make_gmail()
        assert gmail.list_dir_metadata("/") is None

    def test_returns_none_for_label_root(self) -> None:
        gmail = self._make_gmail()
        assert gmail.list_dir_metadata("INBOX") is None

    def test_returns_none_for_unknown_label(self) -> None:
        gmail = self._make_gmail()
        assert gmail.list_dir_metadata("UNKNOWN_LABEL") is None


# ---------------------------------------------------------------------------
# CalendarConnector.list_dir_metadata
# ---------------------------------------------------------------------------


class TestCalendarListDirMetadata:
    """CalendarConnector.list_dir_metadata returns batch metadata."""

    def test_returns_none_for_root(self) -> None:
        from nexus.backends.connectors.gws.connector import CalendarConnector

        cal = CalendarConnector.__new__(CalendarConnector)
        cal._calendar_names = {}
        assert cal.list_dir_metadata("/") is None


# ---------------------------------------------------------------------------
# CalendarConnector.display_path with human-readable calendar names
# ---------------------------------------------------------------------------


class TestCalendarDisplayPath:
    """CalendarConnector.display_path uses human-readable folder names."""

    def test_display_path_with_summary_and_datetime(self) -> None:
        from nexus.backends.connectors.gws.connector import CalendarConnector

        cal = CalendarConnector.__new__(CalendarConnector)
        cal._calendar_names = {"primary": "My Calendar"}

        meta = {
            "summary": "Team Standup",
            "calendarId": "primary",
            "start": {"dateTime": "2026-03-21T10:00:00-07:00"},
        }
        result = cal.display_path("event123", meta)
        assert result.startswith("My-Calendar/2026-03/")
        assert "Team-Standup" in result
        assert result.endswith(".yaml")

    def test_display_path_with_all_day_event(self) -> None:
        from nexus.backends.connectors.gws.connector import CalendarConnector

        cal = CalendarConnector.__new__(CalendarConnector)
        cal._calendar_names = {}

        meta = {
            "summary": "Holiday",
            "calendarId": "primary",
            "start": {"date": "2026-12-25"},
        }
        result = cal.display_path("event456", meta)
        assert "primary/" in result
        assert "Holiday" in result

    def test_display_path_fallback_without_metadata(self) -> None:
        from nexus.backends.connectors.gws.connector import CalendarConnector

        cal = CalendarConnector.__new__(CalendarConnector)
        cal._calendar_names = {}

        result = cal.display_path("event789")
        assert result == "primary/event789.yaml"

    def test_display_path_uses_sanitized_calendar_name(self) -> None:
        from nexus.backends.connectors.gws.connector import CalendarConnector

        cal = CalendarConnector.__new__(CalendarConnector)
        cal._calendar_names = {"user@example.com": "Work: Important Meetings"}

        meta = {
            "summary": "Standup",
            "calendarId": "user@example.com",
            "start": {"dateTime": "2026-04-01T09:00:00Z"},
        }
        result = cal.display_path("evt1", meta)
        # Calendar name should be sanitized (no colons)
        assert ":" not in result.split("/")[0]
        assert "Standup" in result
