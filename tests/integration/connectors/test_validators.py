"""Characterization tests for email and datetime validators.

Written BEFORE refactoring to contracts/validators.py (Issue #2085).
These tests lock down current behavior so the refactoring can be
validated against them.
"""

import re

import pytest

# Current email pattern from gmail/schemas.py
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Current ISO 8601 pattern from calendar/schemas.py
ISO8601_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")

# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------


class TestEmailPatternValid:
    """Valid email addresses that should match."""

    @pytest.mark.parametrize(
        "email",
        [
            "user@example.com",
            "alice@company.org",
            "first.last@domain.co.uk",
            "user+tag@example.com",
            "user_name@example.com",
            "user-name@example.com",
            "user%name@example.com",
            "a@b.co",
            "123@numbers.com",
        ],
    )
    def test_valid_emails(self, email: str) -> None:
        assert EMAIL_PATTERN.match(email) is not None, f"Should match: {email}"

    @pytest.mark.parametrize(
        "email",
        [
            "",
            "plaintext",
            "@missing-local.com",
            "user@",
            "user@.com",
            "user@com",
            "user@@double.com",
            "user@domain",
            " user@example.com",
            "user @example.com",
        ],
    )
    def test_invalid_emails(self, email: str) -> None:
        assert EMAIL_PATTERN.match(email) is None, f"Should NOT match: {email}"


class TestEmailListValidation:
    """Test the validate_email_list logic (currently duplicated 3x in gmail/schemas.py)."""

    def _validate_email_list(self, emails: list[str] | None) -> list[str] | None:
        """Reproduce current validate_email_list logic from gmail/schemas.py."""
        if emails is None:
            return None
        validated = []
        for email in emails:
            if not EMAIL_PATTERN.match(email):
                raise ValueError(f"Invalid email address: {email}")
            validated.append(email.lower())
        return validated

    def test_none_returns_none(self) -> None:
        assert self._validate_email_list(None) is None

    def test_valid_list(self) -> None:
        result = self._validate_email_list(["Alice@Example.COM", "bob@test.org"])
        assert result == ["alice@example.com", "bob@test.org"]

    def test_empty_list(self) -> None:
        result = self._validate_email_list([])
        assert result == []

    def test_invalid_email_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid email address"):
            self._validate_email_list(["valid@test.com", "not-an-email"])

    def test_lowercases_output(self) -> None:
        result = self._validate_email_list(["USER@DOMAIN.COM"])
        assert result == ["user@domain.com"]


# ---------------------------------------------------------------------------
# ISO 8601 datetime validation
# ---------------------------------------------------------------------------


class TestISO8601PatternValid:
    """Valid ISO 8601 datetimes that should match."""

    @pytest.mark.parametrize(
        "dt",
        [
            "2024-01-15T09:00:00Z",
            "2024-01-15T09:00:00+00:00",
            "2024-01-15T09:00:00-08:00",
            "2024-01-15T09:00:00+05:30",
            "2024-12-31T23:59:59Z",
            "2025-06-15T12:00:00-07:00",
        ],
    )
    def test_valid_datetimes(self, dt: str) -> None:
        assert ISO8601_PATTERN.match(dt) is not None, f"Should match: {dt}"

    @pytest.mark.parametrize(
        "dt",
        [
            "",
            "2024-01-15",
            "2024-01-15T09:00:00",
            "2024-01-15 09:00:00Z",
            "not-a-date",
            "2024-01-15T09:00:00.123Z",
            "2024-01-15T09:00:00+8:00",
            "2024-1-15T09:00:00Z",
        ],
    )
    def test_invalid_datetimes(self, dt: str) -> None:
        assert ISO8601_PATTERN.match(dt) is None, f"Should NOT match: {dt}"
