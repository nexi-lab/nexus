"""Output formatters for MCP server responses.

This module provides utilities for formatting MCP tool responses in different
formats (JSON, Markdown) to optimize for token usage and readability.
"""

from __future__ import annotations

import json
from typing import Any


def format_response(data: Any, response_format: str = "json") -> str:
    """Format data as JSON or Markdown.

    Args:
        data: Data to format (dict, list, or simple types)
        response_format: Output format - "json" or "markdown" (default: "json")

    Returns:
        Formatted string

    Example:
        >>> data = {"total": 10, "items": [{"path": "/file1.txt", "size": 100}]}
        >>> print(format_response(data, "json"))
        {
          "total": 10,
          "items": [{"path": "/file1.txt", "size": 100}]
        }
        >>> print(format_response(data, "markdown"))
        **Total**: 10
        ### 1. /file1.txt
        - **size**: 100
    """
    if response_format == "markdown":
        return format_as_markdown(data)
    return json.dumps(data, indent=2, default=str)


def format_as_markdown(data: Any) -> str:
    """Convert data to readable Markdown format.

    Handles paginated responses, lists, and dictionaries with intelligent
    formatting to make output more readable for LLMs.

    Args:
        data: Data to convert (dict, list, or simple types)

    Returns:
        Markdown-formatted string

    Example:
        >>> data = {
        ...     "total": 150,
        ...     "count": 2,
        ...     "offset": 0,
        ...     "items": [
        ...         {"path": "/file1.txt", "size": 100},
        ...         {"path": "/file2.txt", "size": 200}
        ...     ],
        ...     "has_more": True,
        ...     "next_offset": 2
        ... }
        >>> print(format_as_markdown(data))
        **Total**: 150 | **Count**: 2 | **Offset**: 0
        _More results available (next offset: 2)_

        ### 1. /file1.txt
        - **size**: 100

        ### 2. /file2.txt
        - **size**: 200
    """
    if isinstance(data, dict):
        # Check if it's a paginated response
        if "items" in data and "total" in data:
            return _format_paginated_response(data)
        # Single dict, format as key-value pairs
        return _format_dict(data)
    elif isinstance(data, list):
        return _format_list(data)
    else:
        return str(data)


def _format_paginated_response(data: dict) -> str:
    """Format a paginated response with metadata header.

    Args:
        data: Paginated response with total, count, offset, items, has_more, next_offset

    Returns:
        Markdown-formatted string with pagination metadata
    """
    lines = [
        f"**Total**: {data['total']} | **Count**: {data['count']} | **Offset**: {data['offset']}"
    ]

    if data.get("has_more"):
        lines.append(f"_More results available (next offset: {data['next_offset']})_")

    lines.append("")  # Empty line before items

    items = data["items"]
    if isinstance(items, list):
        if not items:
            lines.append("_No items_")
        elif isinstance(items[0], dict):
            lines.extend(_format_list_of_dicts(items))
        else:
            # Simple list of values
            lines.extend([f"- {item}" for item in items])
    else:
        # Items is a dict
        lines.extend([f"**{k}**: {v}" for k, v in items.items()])

    return "\n".join(lines)


def _format_dict(data: dict) -> str:
    """Format a dictionary as Markdown key-value pairs.

    Args:
        data: Dictionary to format

    Returns:
        Markdown-formatted string
    """
    return "\n".join([f"**{k}**: {v}" for k, v in data.items()])


def _format_list(data: list) -> str:
    """Format a list as Markdown.

    Args:
        data: List to format

    Returns:
        Markdown-formatted string
    """
    if not data:
        return "_No items_"

    if isinstance(data[0], dict):
        return "\n\n".join(_format_list_of_dicts(data))
    return "\n".join([f"- {item}" for item in data])


def _format_list_of_dicts(items: list[dict]) -> list[str]:
    """Format a list of dictionaries as numbered Markdown entries.

    Each dict is formatted as a numbered section with key-value pairs.
    Intelligently extracts path/file/name as the section header.

    Args:
        items: List of dictionaries to format

    Returns:
        List of Markdown-formatted strings

    Example:
        >>> items = [
        ...     {"path": "/file1.txt", "size": 100, "modified": "2024-01-01"},
        ...     {"path": "/file2.txt", "size": 200, "modified": "2024-01-02"}
        ... ]
        >>> lines = _format_list_of_dicts(items)
        >>> print("\\n".join(lines))
        ### 1. /file1.txt
        - **size**: 100
        - **modified**: 2024-01-01

        ### 2. /file2.txt
        - **size**: 200
        - **modified**: 2024-01-02
    """
    lines = []
    for i, item in enumerate(items, 1):
        # Try to find a good header: path > file > name > "Item"
        header = item.get("path") or item.get("file") or item.get("name") or f"Item {i}"
        lines.append(f"### {i}. {header}")

        # Add remaining key-value pairs
        for key, value in item.items():
            if key not in ["path", "file", "name"]:  # Skip the key we used in header
                lines.append(f"- **{key}**: {value}")

        # Add empty line between items (but not after the last one)
        if i < len(items):
            lines.append("")

    return lines
