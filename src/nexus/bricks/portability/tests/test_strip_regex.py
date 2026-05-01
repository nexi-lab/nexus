"""Tests for regex backstop credential stripper."""

import pytest

from nexus.bricks.portability.strip import (
    DEFAULT_REDACT_PATTERNS,
    RegexStripper,
)


@pytest.mark.parametrize(
    "secret,name",
    [
        ("sk-ant-aaaaaaaaaaaaaaaaaaaa", "anthropic"),
        ("sk-aaaaaaaaaaaaaaaaaaaa", "openai"),
        ("ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "github_pat"),
        ("gho_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "github_oauth"),
        ("glpat-aaaaaaaaaaaaaaaaaaaa", "gitlab_pat"),
        ("xoxb-1234-5678-aaaaaaaaaa", "slack_bot"),
        ("AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
        ("AIzaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "google_api_key"),
    ],
)
def test_default_patterns_redact_known_secrets(secret, name):
    stripper = RegexStripper(DEFAULT_REDACT_PATTERNS)
    text = f"Use this token: {secret} for auth"
    result = stripper.scan(text, location="docs:42")
    assert "***REDACTED***" in result.text
    assert secret not in result.text
    assert any(m.pattern_name == name for m in result.matches)


def test_no_match_passes_through_unchanged():
    stripper = RegexStripper(DEFAULT_REDACT_PATTERNS)
    text = "regular content with no secrets"
    result = stripper.scan(text, location="docs:1")
    assert result.text == text
    assert result.matches == []


def test_custom_pattern_applies():
    stripper = RegexStripper([{"name": "corp_token", "pattern": r"corp-[A-Z0-9]{8}"}])
    result = stripper.scan("token=corp-AB12CD34", location="settings:1")
    assert "***REDACTED***" in result.text
    assert result.matches[0].pattern_name == "corp_token"


def test_invalid_regex_raises_at_construction():
    with pytest.raises(ValueError):
        RegexStripper([{"name": "bad", "pattern": "[unclosed"}])
