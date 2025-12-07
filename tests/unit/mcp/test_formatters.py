"""Tests for MCP formatters.

These tests verify the output formatters for MCP server responses.
"""

import json

from nexus.mcp.formatters import (
    _format_dict,
    _format_list,
    _format_list_of_dicts,
    _format_paginated_response,
    format_as_markdown,
    format_response,
)


class TestFormatResponse:
    """Test format_response function."""

    def test_format_response_json_default(self) -> None:
        """Test default JSON formatting."""
        data = {"key": "value", "count": 10}
        result = format_response(data)
        assert json.loads(result) == data

    def test_format_response_json_explicit(self) -> None:
        """Test explicit JSON formatting."""
        data = {"items": [1, 2, 3]}
        result = format_response(data, response_format="json")
        assert json.loads(result) == data

    def test_format_response_markdown(self) -> None:
        """Test markdown formatting."""
        data = {"key": "value"}
        result = format_response(data, response_format="markdown")
        assert "**key**:" in result
        assert "value" in result

    def test_format_response_complex_types(self) -> None:
        """Test JSON formatting with complex types."""
        from datetime import datetime

        data = {"timestamp": datetime(2024, 1, 1, 12, 0, 0)}
        result = format_response(data, response_format="json")
        # datetime should be converted to string via default=str
        parsed = json.loads(result)
        assert "2024-01-01" in parsed["timestamp"]


class TestFormatAsMarkdown:
    """Test format_as_markdown function."""

    def test_format_paginated_response(self) -> None:
        """Test formatting paginated response."""
        data = {
            "total": 100,
            "count": 2,
            "offset": 0,
            "items": [
                {"path": "/file1.txt", "size": 100},
                {"path": "/file2.txt", "size": 200},
            ],
            "has_more": True,
            "next_offset": 2,
        }
        result = format_as_markdown(data)
        assert "**Total**: 100" in result
        assert "**Count**: 2" in result
        assert "**Offset**: 0" in result
        assert "More results available" in result
        assert "/file1.txt" in result
        assert "/file2.txt" in result

    def test_format_simple_dict(self) -> None:
        """Test formatting simple dictionary."""
        data = {"name": "test", "value": 42}
        result = format_as_markdown(data)
        assert "**name**: test" in result
        assert "**value**: 42" in result

    def test_format_simple_list(self) -> None:
        """Test formatting simple list."""
        data = ["item1", "item2", "item3"]
        result = format_as_markdown(data)
        assert "- item1" in result
        assert "- item2" in result
        assert "- item3" in result

    def test_format_list_of_dicts(self) -> None:
        """Test formatting list of dictionaries."""
        data = [
            {"path": "/a.txt", "size": 10},
            {"path": "/b.txt", "size": 20},
        ]
        result = format_as_markdown(data)
        assert "### 1. /a.txt" in result
        assert "### 2. /b.txt" in result
        assert "**size**: 10" in result
        assert "**size**: 20" in result

    def test_format_empty_list(self) -> None:
        """Test formatting empty list."""
        result = format_as_markdown([])
        assert "_No items_" in result

    def test_format_scalar(self) -> None:
        """Test formatting scalar value."""
        assert format_as_markdown("hello") == "hello"
        assert format_as_markdown(42) == "42"
        assert format_as_markdown(3.14) == "3.14"


class TestFormatPaginatedResponse:
    """Test _format_paginated_response function."""

    def test_with_more_results(self) -> None:
        """Test paginated response with more results available."""
        data = {
            "total": 50,
            "count": 10,
            "offset": 0,
            "items": [{"path": "/test.txt"}],
            "has_more": True,
            "next_offset": 10,
        }
        result = _format_paginated_response(data)
        assert "_More results available (next offset: 10)_" in result

    def test_without_more_results(self) -> None:
        """Test paginated response with no more results."""
        data = {
            "total": 5,
            "count": 5,
            "offset": 0,
            "items": [],
            "has_more": False,
        }
        result = _format_paginated_response(data)
        assert "_More results available" not in result
        assert "_No items_" in result

    def test_items_as_dict(self) -> None:
        """Test paginated response with items as dict."""
        data = {
            "total": 2,
            "count": 2,
            "offset": 0,
            "items": {"key1": "value1", "key2": "value2"},
        }
        result = _format_paginated_response(data)
        assert "**key1**: value1" in result
        assert "**key2**: value2" in result

    def test_simple_list_items(self) -> None:
        """Test paginated response with simple list items."""
        data = {
            "total": 3,
            "count": 3,
            "offset": 0,
            "items": ["one", "two", "three"],
        }
        result = _format_paginated_response(data)
        assert "- one" in result
        assert "- two" in result
        assert "- three" in result


class TestFormatDict:
    """Test _format_dict function."""

    def test_basic_dict(self) -> None:
        """Test basic dictionary formatting."""
        data = {"a": 1, "b": "two"}
        result = _format_dict(data)
        assert "**a**: 1" in result
        assert "**b**: two" in result

    def test_empty_dict(self) -> None:
        """Test empty dictionary formatting."""
        result = _format_dict({})
        assert result == ""


class TestFormatList:
    """Test _format_list function."""

    def test_empty_list(self) -> None:
        """Test empty list returns no items message."""
        result = _format_list([])
        assert result == "_No items_"

    def test_simple_list(self) -> None:
        """Test simple list formatting."""
        result = _format_list(["a", "b", "c"])
        assert "- a" in result
        assert "- b" in result
        assert "- c" in result

    def test_list_of_dicts(self) -> None:
        """Test list of dicts formatting."""
        result = _format_list([{"name": "foo"}])
        assert "### 1. foo" in result


class TestFormatListOfDicts:
    """Test _format_list_of_dicts function."""

    def test_uses_path_as_header(self) -> None:
        """Test that path is used as header."""
        items = [{"path": "/my/path.txt", "size": 100}]
        result = _format_list_of_dicts(items)
        assert "### 1. /my/path.txt" in result[0]
        assert "**size**: 100" in result[1]

    def test_uses_file_as_header(self) -> None:
        """Test that file is used as header if no path."""
        items = [{"file": "test.txt", "type": "txt"}]
        result = _format_list_of_dicts(items)
        assert "### 1. test.txt" in result[0]
        assert "**type**: txt" in result[1]

    def test_uses_name_as_header(self) -> None:
        """Test that name is used as header if no path/file."""
        items = [{"name": "TestItem", "value": 42}]
        result = _format_list_of_dicts(items)
        assert "### 1. TestItem" in result[0]

    def test_uses_item_number_as_fallback(self) -> None:
        """Test that item number is used as fallback header."""
        items = [{"key": "value", "count": 5}]
        result = _format_list_of_dicts(items)
        assert "### 1. Item 1" in result[0]

    def test_multiple_items(self) -> None:
        """Test formatting multiple items."""
        items = [
            {"path": "/a.txt"},
            {"path": "/b.txt"},
            {"path": "/c.txt"},
        ]
        result = _format_list_of_dicts(items)
        lines = result
        assert any("### 1. /a.txt" in line for line in lines)
        assert any("### 2. /b.txt" in line for line in lines)
        assert any("### 3. /c.txt" in line for line in lines)

    def test_empty_list(self) -> None:
        """Test formatting empty list."""
        result = _format_list_of_dicts([])
        assert result == []
