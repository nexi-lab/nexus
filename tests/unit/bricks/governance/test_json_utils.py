"""Unit tests for governance JSON parsing helpers.

Tests parse_json_metadata() with valid JSON, malformed JSON, None, empty
strings, and non-dict JSON values.
"""

from __future__ import annotations

import pytest

from nexus.bricks.governance.json_utils import parse_json_metadata


class TestParseJsonMetadata:
    """Tests for parse_json_metadata()."""

    def test_valid_json_dict(self) -> None:
        result = parse_json_metadata('{"key": "value", "count": 42}')
        assert result == {"key": "value", "count": 42}

    def test_nested_json_dict(self) -> None:
        raw = '{"outer": {"inner": [1, 2, 3]}}'
        result = parse_json_metadata(raw)
        assert result == {"outer": {"inner": [1, 2, 3]}}

    def test_empty_json_object(self) -> None:
        assert parse_json_metadata("{}") == {}

    def test_none_returns_empty_dict(self) -> None:
        assert parse_json_metadata(None) == {}

    def test_empty_string_returns_empty_dict(self) -> None:
        assert parse_json_metadata("") == {}

    def test_malformed_json_returns_empty_dict(self) -> None:
        assert parse_json_metadata("{not valid json}") == {}

    def test_json_array_returns_empty_dict(self) -> None:
        """Non-dict JSON (e.g. array) should return empty dict."""
        assert parse_json_metadata("[1, 2, 3]") == {}

    def test_json_string_returns_empty_dict(self) -> None:
        """JSON string literal should return empty dict."""
        assert parse_json_metadata('"just a string"') == {}

    def test_json_number_returns_empty_dict(self) -> None:
        """JSON number should return empty dict."""
        assert parse_json_metadata("42") == {}

    def test_json_null_returns_empty_dict(self) -> None:
        """JSON null should return empty dict."""
        assert parse_json_metadata("null") == {}

    @pytest.mark.parametrize(
        "raw",
        [
            '{"severity": "high"}',
            '{"score": 0.95, "components": {"ring": 0.8}}',
            '{"agents": ["a1", "a2"]}',
        ],
        ids=["severity-only", "score-with-components", "agents-list"],
    )
    def test_various_valid_metadata(self, raw: str) -> None:
        """Various real-world metadata shapes should parse correctly."""
        result = parse_json_metadata(raw)
        assert isinstance(result, dict)
        assert len(result) > 0
