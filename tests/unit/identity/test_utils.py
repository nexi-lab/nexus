"""Tests for identity utility functions (Issue #1355, Decision #5B)."""

from __future__ import annotations

import types

import pytest

from nexus.identity.utils import parse_metadata, serialize_metadata


class TestParseMetadata:
    def test_none_returns_empty(self) -> None:
        result = parse_metadata(None)
        assert result == {}
        assert isinstance(result, types.MappingProxyType)

    def test_valid_json(self) -> None:
        result = parse_metadata('{"key": "value"}')
        assert result["key"] == "value"
        assert isinstance(result, types.MappingProxyType)

    def test_invalid_json(self) -> None:
        result = parse_metadata("not json", context="test-agent")
        assert result == {}

    def test_non_dict_json(self) -> None:
        result = parse_metadata("[1, 2, 3]", context="test-agent")
        assert result == {}

    def test_immutable(self) -> None:
        result = parse_metadata('{"key": "value"}')
        with pytest.raises(TypeError):
            result["key"] = "new"

    def test_nested_dict(self) -> None:
        result = parse_metadata('{"a": {"b": 1}}')
        assert result["a"]["b"] == 1


class TestSerializeMetadata:
    def test_none_returns_none(self) -> None:
        assert serialize_metadata(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert serialize_metadata({}) is None

    def test_valid_dict(self) -> None:
        result = serialize_metadata({"key": "value"})
        assert result is not None
        assert '"key"' in result
        assert '"value"' in result

    def test_sorted_keys(self) -> None:
        result = serialize_metadata({"b": 2, "a": 1})
        assert result is not None
        # Keys should be sorted
        assert result.index('"a"') < result.index('"b"')

    def test_compact_format(self) -> None:
        """No extra whitespace (compact separators)."""
        result = serialize_metadata({"key": "value"})
        assert result is not None
        assert " " not in result
