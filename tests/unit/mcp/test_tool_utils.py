"""Tests for MCP tool utilities — error envelope + decorator (Issue #1272).

Tests cover:
- tool_error() formatting and logging
- handle_tool_errors() decorator exception handling
- Decorator preserves function signature
- Path extraction from arguments
"""

from __future__ import annotations

import inspect
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.mcp.tool_utils import (
    _extract_path_hint,
    handle_tool_errors,
    tool_error,
)


# ---------------------------------------------------------------------------
# tool_error()
# ---------------------------------------------------------------------------


class TestToolError:
    def test_returns_error_prefix(self):
        result = tool_error("not_found", "File not found at '/data.txt'")
        assert result == "Error: File not found at '/data.txt'"

    def test_format_is_consistent(self):
        """All categories produce the same 'Error: ...' format."""
        for category in ["not_found", "permission_denied", "invalid_input", "internal"]:
            result = tool_error(category, "test message")
            assert result.startswith("Error: ")

    def test_detail_not_in_response(self):
        """Server-side detail must never leak to the agent."""
        result = tool_error(
            "internal",
            "Something went wrong",
            detail="Traceback: ZeroDivisionError at line 42",
        )
        assert "Traceback" not in result
        assert "ZeroDivisionError" not in result
        assert "line 42" not in result
        assert result == "Error: Something went wrong"

    def test_detail_logged_at_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="nexus.mcp.tool_utils"):
            tool_error("internal", "oops", detail="stack trace here")
        assert "stack trace here" in caplog.text
        assert "internal" in caplog.text

    def test_no_detail_logged_at_debug(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="nexus.mcp.tool_utils"):
            tool_error("not_found", "File not found")
        assert "not_found" in caplog.text
        assert "File not found" in caplog.text


# ---------------------------------------------------------------------------
# handle_tool_errors()
# ---------------------------------------------------------------------------


class TestHandleToolErrors:
    def test_passes_through_on_success(self):
        @handle_tool_errors("testing")
        def my_tool(path: str) -> str:
            return f"ok: {path}"

        assert my_tool("/data.txt") == "ok: /data.txt"

    def test_catches_file_not_found_error(self):
        @handle_tool_errors("reading file")
        def my_tool(path: str) -> str:
            raise FileNotFoundError(f"No such file: {path}")

        result = my_tool("/missing.txt")
        assert result.startswith("Error:")
        assert "not found" in result.lower()
        assert "/missing.txt" in result

    def test_catches_permission_error(self):
        @handle_tool_errors("writing file")
        def my_tool(path: str) -> str:
            raise PermissionError(f"Access denied: {path}")

        result = my_tool("/secret.txt")
        assert result.startswith("Error:")
        assert "Permission denied" in result
        assert "/secret.txt" in result

    def test_catches_generic_exception(self):
        @handle_tool_errors("processing data")
        def my_tool(path: str) -> str:
            raise RuntimeError("database timeout")

        result = my_tool("/data.txt")
        assert result.startswith("Error:")
        assert "processing data" in result.lower()
        assert "database timeout" in result

    def test_generic_error_does_not_include_stack_trace(self):
        @handle_tool_errors("processing")
        def my_tool(path: str) -> str:
            raise ValueError("bad value")

        result = my_tool("/data.txt")
        assert "Traceback" not in result
        assert "ValueError" not in result  # Class name not in user-facing message

    def test_preserves_function_signature(self):
        """FastMCP inspects signatures — decorator must preserve them."""

        @handle_tool_errors("testing")
        def my_tool(path: str, limit: int = 10, ctx: Any = None) -> str:
            return "ok"

        sig = inspect.signature(my_tool)
        param_names = list(sig.parameters.keys())
        assert param_names == ["path", "limit", "ctx"]
        assert sig.parameters["limit"].default == 10

    def test_preserves_function_name(self):
        @handle_tool_errors("testing")
        def nexus_read_file(path: str) -> str:
            return "ok"

        assert nexus_read_file.__name__ == "nexus_read_file"

    def test_works_with_kwargs(self):
        @handle_tool_errors("reading file")
        def my_tool(path: str, ctx: Any = None) -> str:
            raise FileNotFoundError(path)

        result = my_tool(path="/workspace/file.txt", ctx=None)
        assert "/workspace/file.txt" in result

    def test_file_not_found_with_no_path_arg(self):
        """When no path argument is present, error should still be clean."""

        @handle_tool_errors("listing servers")
        def my_tool(ctx: Any = None) -> str:
            raise FileNotFoundError("config missing")

        result = my_tool()
        assert result.startswith("Error:")
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# _extract_path_hint()
# ---------------------------------------------------------------------------


class TestExtractPathHint:
    def test_extracts_from_kwargs(self):
        assert _extract_path_hint((), {"path": "/data.txt"}) == "/data.txt"

    def test_extracts_from_first_positional_arg(self):
        assert _extract_path_hint(("/data.txt",), {}) == "/data.txt"

    def test_returns_none_for_non_path_first_arg(self):
        assert _extract_path_hint(("hello",), {}) is None

    def test_returns_none_for_no_args(self):
        assert _extract_path_hint((), {}) is None

    def test_kwargs_takes_precedence(self):
        assert (
            _extract_path_hint(("/arg_path",), {"path": "/kwarg_path"})
            == "/kwarg_path"
        )
